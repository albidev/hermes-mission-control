"""Durable owner-private AES-GCM receipts, one immutable file per scope.

Walk from a trusted anchor using O_DIRECTORY|O_NOFOLLOW and retain the directory fd.
All key/lock/receipt/staging IO is descriptor-relative. Readers validate and read the
same O_NOFOLLOW fd (regular, owner UID, 0600, one link). Writers fsync a private
exclusive staging fd, publish by no-clobber link, verify its inode against the still
open written fd, unlink staging and fsync the directory. A pathname ABA cannot
redirect writes: there is no SQLite connection, journal or pathname reopen.

Ancestors above the configured anchor are trusted. Root pathname identity is an
additional fail-closed check, not the mechanism that confines IO. SQLite remnants
are refused without opening them; this unreleased format has no migration.
"""
from __future__ import annotations

import contextvars
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
# No SQLite: every receipt is opened through the retained directory descriptor.
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from invite_rooms.consent import canonical, verify as verify_consent

_DOMAIN = b"mc-material-receipt-v1\0"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC

KEY_NAME = "material_receipts.key"
DB_NAME = "material_receipts.sqlite"
LOCK_NAME = ".material-receipts.lock"

# Propagated by asyncio.to_thread / run_admission to bound every lock wait by the request deadline.
ADMISSION_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("registry_admission_deadline", default=None)
LOCK_WAIT = 2.0          # default cap for an operation-lock wait (never blocks indefinitely)
BOOTSTRAP_LOCK_WAIT = 10.0
STAGING_MAX_AGE = 300.0  # an unpublished staging file older than this is an orphan of a dead writer

# The target side (joining_material.validate_material) already caps the grant at 8192 characters
# before signing, so nothing legitimate is longer; the host mirrors that exact bound.
MAX_GRANT_LEN = 8192


class MaterialReceiptError(ValueError):
    """Sanitized fail-closed receipt error."""


def valid_grant(value) -> bool:
    """Non-empty, bounded, printable ASCII without whitespace (real grants are ``payload.signature``)."""
    return (type(value) is str and 0 < len(value) <= MAX_GRANT_LEN and value.isascii()
            and value.isprintable() and not any(c.isspace() for c in value))


def _refuse():
    raise MaterialReceiptError("private_storage_required")


def _flock_until(fd: int, deadline: float) -> None:
    """Exclusive flock without ever blocking past ``deadline`` (monotonic)."""
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise MaterialReceiptError("receipt_busy") from None
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))


def _check_dir(fd: int, *, private: bool) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        _refuse()
    mode = stat.S_IMODE(st.st_mode)
    if (private and mode != 0o700) or (not private and mode & 0o022):
        _refuse()


def _check_regular(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1:
        _refuse()


def _nearest_existing_parent(root: Path) -> Path:
    """Default anchor when none is configured: where directory creation would begin."""
    candidate = Path(os.path.abspath(root)).parent
    while not os.path.lexists(candidate) and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _open_root(root: Path, anchor: Path) -> int:
    """Return a validated directory fd for ``root`` reached from ``anchor`` without following links."""
    root_abs, anchor_abs = Path(os.path.abspath(root)), Path(os.path.abspath(anchor))
    try:
        parts = root_abs.relative_to(anchor_abs).parts
    except ValueError:
        _refuse()
    try:
        fd = os.open(anchor_abs, _DIR_FLAGS)
    except OSError:
        _refuse()
    try:
        _check_dir(fd, private=not parts)
        for index, name in enumerate(parts):
            created = False
            try:
                nfd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                nfd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if created:
                os.fchmod(fd, 0o700)
            _check_dir(fd, private=index == len(parts) - 1)
        return fd
    except MaterialReceiptError:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        _refuse()
    except BaseException:
        os.close(fd)
        raise


def _validate_material(material) -> None:
    """Exact stored/admitted material schema; raises ValueError (callers map it to a sanitized code)."""
    if (not isinstance(material, dict) or set(material) != {"binding", "catalog", "grant"}
            or not isinstance(material["binding"], dict) or not isinstance(material["catalog"], dict)
            or not valid_grant(material["grant"])):
        raise ValueError()


class MaterialReceiptStore:
    """Immutable per-scope receipt files. The key is never shared with JoiningStore or TLS."""

    def __init__(self, root: str | Path, *, anchor: str | Path | None = None):
        self.root = Path(root)
        self._root_abs = os.path.abspath(self.root)
        self._fd = _open_root(self.root, Path(anchor) if anchor is not None else _nearest_existing_parent(self.root))
        self._lock = threading.RLock()
        self.path = self.root / DB_NAME
        self.key_path = self.root / KEY_NAME
        try:
            st = os.fstat(self._fd)
            self._root_id = (st.st_dev, st.st_ino)

            lock_fd = self._open_regular(LOCK_NAME, create=True)
            try:
                _flock_until(lock_fd, time.monotonic() + BOOTSTRAP_LOCK_WAIT)
                self._check_store()
                self._recover_staging()  # crash remnants must be gone before any nlink==1 check
                initialized = os.pread(lock_fd, 2, 0)
                if initialized not in (b'', b'\x01'):
                    _refuse()
                self._load_key(initialized=bool(initialized))
                # Durable initialized marker on the locked fd replaces the old empty-DB
                # sentinel: losing the key of even an empty initialized store never regenerates it.
                if not initialized:
                    if os.pwrite(lock_fd, b'\x01', 0) != 1:
                        _refuse()
                    os.fsync(lock_fd)
                    os.fsync(self._dirfd())
                self._check_store()
            finally:
                os.close(lock_fd)  # releases the bootstrap lock
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        fd, self._fd = getattr(self, "_fd", None), None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        self.close()

    # -- descriptor-relative primitives -------------------------------------------------------
    def _dirfd(self) -> int:
        if self._fd is None:
            _refuse()
        return self._fd

    def _exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._dirfd(), follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _open_regular(self, name: str, *, create: bool = False, writable: bool = False) -> int:
        """Open ``name`` no-follow relative to the directory fd; fstat type/UID/mode 0600 or refuse."""
        base = (os.O_RDWR if (create or writable) else os.O_RDONLY) | _NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
        fd = None
        try:
            if create:
                try:
                    fd = os.open(name, base | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._dirfd())
                    os.fchmod(fd, 0o600)
                except FileExistsError:
                    fd = None
            if fd is None:
                fd = os.open(name, base, dir_fd=self._dirfd())
            _check_regular(fd)
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
            return fd
        except MaterialReceiptError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError:
            if fd is not None:
                os.close(fd)
            _refuse()

    def _root_unchanged(self) -> bool:
        try:
            st = os.lstat(self._root_abs)
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == self._root_id

    def _recover_staging(self) -> None:
        """Under the flock: drop staging files left by a crashed/aborted publication (R3-01).

        Only producer names ``<KEY|64-lowercase-hex.receipt>.tmp-<32-lowercase-hex>``
        are considered, classified with an lstat and opened
        O_NOFOLLOW|O_NONBLOCK only when regular (symlinks/FIFOs/dirs are never followed or opened and
        are left untouched). A staging inode that is also the published final name (crash between
        link and unlink) is unlinked; a pre-link orphan is unlinked once older than STAGING_MAX_AGE.
        A foreign hardlink on a final name has no staging name and is still refused later."""
        dirfd = self._dirfd()
        removed = False
        for entry in os.listdir(dirfd):
            match = re.fullmatch(r'(' + re.escape(KEY_NAME) + r'|[0-9a-f]{64}\.receipt)\.tmp-[0-9a-f]{32}', entry)
            if match is None:
                continue
            base = match.group(1)
            try:
                before = os.stat(entry, dir_fd=dirfd, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    continue
                fd = os.open(entry, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=dirfd)
            except OSError:
                continue
            try:
                st = os.fstat(fd)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
                        or self._identity(st) != self._identity(before)):
                    continue
                try:
                    published = self._identity(os.stat(base, dir_fd=dirfd, follow_symlinks=False)) == self._identity(st)
                except OSError:
                    published = False
                if published or time.time() - st.st_mtime > STAGING_MAX_AGE:
                    try:
                        os.unlink(entry, dir_fd=dirfd)
                        removed = True
                    except FileNotFoundError:
                        pass
            finally:
                os.close(fd)
        if removed:
            os.fsync(dirfd)

    # -- key bootstrap (R-06) -----------------------------------------------------------------
    def _load_key(self, *, initialized=False):
        key_exists = self._exists(KEY_NAME)
        if not key_exists and (initialized or any(n.endswith('.receipt') for n in os.listdir(self._dirfd()))):
            raise MaterialReceiptError("receipt_key_missing")
        if not key_exists:
            self._bootstrap_key()
        fd = self._open_regular(KEY_NAME)
        try:
            raw = os.read(fd, 33)
            self._check_name(KEY_NAME, fd)
            os.fsync(self._dirfd())
        finally:
            os.close(fd)
        if len(raw) != 32:
            raise MaterialReceiptError("receipt_key_invalid")
        self._aes = AESGCM(raw)

    def _bootstrap_key(self):
        """Publish only a complete, fsynced key; bootstrap lock prevents empty-key races."""
        self._publish(KEY_NAME, AESGCM.generate_key(bit_length=256))

    # -- immutable per-scope files ------------------------------------------------------------
    def _check_store(self):
        _check_dir(self._dirfd(), private=True)
        if not self._root_unchanged() or self._exists(DB_NAME):
            _refuse()  # unreleased SQLite format is deliberately NOT migrated or opened

    @staticmethod
    def receipt_name(scope):
        return hashlib.sha256(canonical(scope.as_dict())).hexdigest() + ".receipt"

    @staticmethod
    def _identity(st):
        return st.st_dev, st.st_ino

    def _check_name(self, name, fd):
        """Bind publication to the still-open staging inode, not just to its pathname."""
        st = os.stat(name, dir_fd=self._dirfd(), follow_symlinks=False)
        if self._identity(st) != self._identity(os.fstat(fd)):
            _refuse()

    def _publish(self, name, data):
        """Exclusive publication; keep the written fd alive through link/unlink/verification/fsync.

        link never follows the source symlink and never replaces the destination. A staging-name
        swap during link is caught by comparing the actual published file with the written fd.
        No writes are made through either of these interchangeable names.
        """
        dirfd = self._dirfd()
        staging = name + ".tmp-" + secrets.token_hex(16)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dirfd)
        try:
            os.fchmod(fd, 0o600)
            _check_regular(fd)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("storage_write_failed")
                view = view[written:]
            os.fsync(fd)
            self._check_name(staging, fd)
            os.link(staging, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
            self._check_name(name, fd)
            os.unlink(staging, dir_fd=dirfd)
            _check_regular(fd)  # after unlink, the published inode must have exactly one link
            self._check_name(name, fd)
            os.fsync(dirfd)
            if not self._root_unchanged():
                _refuse()
        finally:
            os.close(fd)
            try:
                os.unlink(staging, dir_fd=dirfd)
            except FileNotFoundError:
                pass

    def _read_receipt(self, scope):
        self._check_store()
        name = self.receipt_name(scope)
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK, dir_fd=self._dirfd())
        except FileNotFoundError:
            raise MaterialReceiptError("material_scope_unapproved") from None
        except OSError:
            _refuse()
        try:
            _check_regular(fd)
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
            # Read ONLY the file whose descriptor was verified. Never reopen by path.
            raw = bytearray()
            while len(raw) <= 256 * 1024:
                chunk = os.read(fd, min(65536, 256 * 1024 + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 256 * 1024:
                raise ValueError()
            self._check_name(name, fd)
            self._check_store()
            row = json.loads(raw)
            if set(row) != {"scope_key", "generation", "nonce", "ciphertext", "digest"}:
                raise ValueError()
            if row["scope_key"] != self.key_for(scope) or row["generation"] != scope.pairing_generation:
                raise ValueError()
            return (row["generation"], bytes.fromhex(row["nonce"]), bytes.fromhex(row["ciphertext"]), bytes.fromhex(row["digest"]))
        except MaterialReceiptError:
            raise
        except Exception:
            raise MaterialReceiptError("material_unavailable") from None
        finally:
            os.close(fd)

    @staticmethod
    def key_for(scope):
        try: data = scope.as_dict()
        except AttributeError: raise MaterialReceiptError("invalid_material_scope") from None
        return canonical(data).decode()

    @staticmethod
    def _aad(scope):
        return _DOMAIN + canonical(scope.as_dict()) + b"\0" + str(scope.pairing_generation).encode("ascii")

    def admit(self, scope, material: dict, *, deadline=None):
        """Insert the first validated host association; identical is idempotent."""
        key = self.key_for(scope)
        if not isinstance(material, dict) or set(material) != {"binding", "catalog", "grant"} or material["binding"] != scope.binding:
            raise MaterialReceiptError("material_scope_mismatch")
        try:
            _validate_material(material)  # grant/catalog schema BEFORE encryption and INSERT (R-04)
        except ValueError:
            raise MaterialReceiptError("invalid_material") from None
        plaintext = canonical(material)
        digest = hashlib.sha256(plaintext).digest(); nonce = secrets.token_bytes(12)
        encrypted = self._aes.encrypt(nonce, plaintext, self._aad(scope))
        with self._operation(deadline):
            self._check_store()
            try:
                row = self._read_receipt(scope)
            except MaterialReceiptError as exc:
                if str(exc) != "material_scope_unapproved":
                    raise
            else:
                self._check_identical(scope, row, plaintext)
                os.fsync(self._dirfd())  # retry after a failed publication dir-fsync must be durable
                return
            data = canonical(dict(scope_key=key, generation=scope.pairing_generation,
                                  nonce=nonce.hex(), ciphertext=encrypted.hex(), digest=digest.hex()))
            try:
                self._publish(self.receipt_name(scope), data)
            except FileExistsError:
                # Exclusive publication is the first-receipt-wins boundary. Never overwrite.
                self._check_identical(scope, self._read_receipt(scope), plaintext)
            except OSError:
                _refuse()
            self._check_store()

    @contextmanager
    def _operation(self, deadline=None):
        """Process lock + flock, both bounded by min(LOCK_WAIT, request deadline, ``deadline``).

        ``deadline`` is an ADMISSION bound, not merely an acquisition-wait cap (R3B-02): expiry is
        re-checked after the mutex acquire, after the flock acquire and before the caller's first
        effect, so an already-expired request can never publish just because both locks were free.
        This bounds ONE operation: it creates no durable claim, and expiry revokes nothing that a
        previous operation already committed.
        """
        limit = time.monotonic() + LOCK_WAIT
        for extra in (deadline, ADMISSION_DEADLINE.get()):
            if extra is not None:
                limit = min(limit, extra)
        if not self._lock.acquire(timeout=max(0.0, limit - time.monotonic())):
            raise MaterialReceiptError("receipt_busy")
        try:
            if time.monotonic() >= limit:
                raise MaterialReceiptError("receipt_busy")  # expired while queued for the mutex
            fd = self._open_regular(LOCK_NAME)
            try:
                _flock_until(fd, limit)
                if time.monotonic() >= limit:
                    raise MaterialReceiptError("receipt_busy")  # expired between mutex and flock
                yield
            finally:
                os.close(fd)
        finally:
            self._lock.release()

    def _check_identical(self, scope, row, plaintext):
        try:
            existing = self._aes.decrypt(row[1], row[2], self._aad(scope))
        except Exception:
            raise MaterialReceiptError("material_unavailable") from None
        if row[0] != scope.pairing_generation or hashlib.sha256(existing).digest() != row[3] or existing != plaintext:
            raise MaterialReceiptError("material_scope_conflict")

    def load(self, scope, *, deadline=None) -> dict:
        self.key_for(scope)
        with self._operation(deadline):
            row = self._read_receipt(scope)
        if row[0] != scope.pairing_generation: raise MaterialReceiptError("material_scope_unapproved")
        try:
            raw = self._aes.decrypt(row[1], row[2], self._aad(scope))
            if hashlib.sha256(raw).digest() != row[3]: raise ValueError()
            value = json.loads(raw)
            _validate_material(value)  # defense in depth: never hand back a malformed record
            return value
        except Exception: raise MaterialReceiptError("material_unavailable") from None

    def admit_envelope(self, *, scope, envelope: dict, consent_public_key: bytes, expected_signed: dict, map_scope=None, guard=None):
        """Verify the signed envelope exactly as received, then map/validate it against the local scope.

        Order: exact schema -> signature over the received bytes -> scope mapping -> grant -> catalog
        -> ``guard``.  Nothing is encrypted or inserted unless every step passes, and ``guard`` runs
        immediately before the first effect so a caller can restate an authoritative precondition
        (R3B-05) after all verification has completed.
        """
        if (not isinstance(envelope, dict) or set(envelope) != {"scope", "grant", "catalog", "signature"}
                or not isinstance(envelope["scope"], dict) or not isinstance(envelope["catalog"], dict)
                or not isinstance(envelope["signature"], str)):
            raise MaterialReceiptError("invalid_consent")
        signed = {"scope": envelope["scope"], "grant": envelope["grant"], "catalog": envelope["catalog"], "purpose": "joining-owner-material-v1"}
        if expected_signed != signed:
            raise MaterialReceiptError("invalid_consent")
        try: verify_consent(consent_public_key, signed, envelope["signature"], signed)
        except Exception: raise MaterialReceiptError("invalid_consent") from None
        if map_scope is not None: map_scope(envelope["scope"], scope)
        if not valid_grant(envelope["grant"]):
            raise MaterialReceiptError("invalid_material")
        try:
            from gateway.hosted_room_peer import GatewayRoomCatalog
            catalog = GatewayRoomCatalog.from_mapping(copy.deepcopy(envelope["catalog"]))
            if (not catalog.text or catalog.attachments or catalog.execution_policy.approval_mode != "manual"
                    or tuple(sorted(catalog.execution_policy.enabled_toolsets)) != ("bot_room",)
                    or catalog.installation_id != scope.target_install_id
                    or catalog.execution_policy.target_profile != scope.target_profile
                    or catalog.catalog_digest != scope.catalog_digest
                    or catalog.execution_policy.policy_digest != scope.policy_digest):
                raise ValueError()
        except MaterialReceiptError:
            raise
        except Exception: raise MaterialReceiptError("invalid_catalog") from None
        material = {"binding": scope.binding, "catalog": copy.deepcopy(envelope["catalog"]), "grant": envelope["grant"]}
        if guard is not None:
            guard()  # last authoritative re-statement, with no effect yet (R3B-05)
        self.admit(scope, material)
