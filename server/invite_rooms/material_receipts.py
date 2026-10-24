"""Durable, owner-private encrypted host material receipts (P3b.0b, hardened in P3b.0-F).

Storage model (R-03/R-06)
-------------------------
* The receipt directory is opened ONCE as a directory descriptor (``O_DIRECTORY|O_NOFOLLOW``)
  by walking from a trusted *anchor* (production: the parent of ``store_path`` under
  ``~/.config/mc-room-invites``; default when none is given: the nearest existing parent).  Every component from the anchor
  down is opened no-follow relative to its parent descriptor and fstat-checked.  Ancestors ABOVE
  the anchor (``/Users``, ``/var`` -> ``/private/var`` ...) are deliberately not our business.
* The key, the lock, the staging key and the database are created/opened relative to that
  descriptor (``dir_fd=``), no-follow, with fstat type/UID/mode checks.  The pathname of the
  receipt root is never trusted again for key/lock/staging I/O.
* SQLite only accepts a pathname.  Every connection therefore (a) opens a no-follow descriptor on
  the database relative to the retained directory fd, (b) refuses unless it is the exact inode
  validated at initialisation, (c) refuses unless the receipt-root pathname still resolves to the
  retained directory, and (d) repeats the inode/root check immediately after ``connect``.
  (``/dev/fd/<dirfd>/name`` is not an option on macOS: fdescfs exposes no directory traversal,
  and SQLite also needs to create ``-journal`` siblings by path.)
* The key is generated, written to a private no-follow staging file, fsynced, published by
  ``link`` (never overwrites) and the directory is fsynced, all under the bootstrap lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
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


def _check_dir(fd: int, *, private: bool) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        _refuse()
    mode = stat.S_IMODE(st.st_mode)
    if (private and mode != 0o700) or (not private and mode & 0o022):
        _refuse()


def _check_regular(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
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
    """SQLite receipt store. The key is never shared with JoiningStore or TLS."""

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
            self._db_id = None
            lock_fd = self._open_regular(LOCK_NAME, create=True)
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                self._load_key()
                self._init_db()
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
        base = (os.O_RDWR if (create or writable) else os.O_RDONLY) | _NOFOLLOW | _CLOEXEC
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

    # -- key bootstrap (R-06) -----------------------------------------------------------------
    def _load_key(self):
        key_exists, db_exists = self._exists(KEY_NAME), self._exists(DB_NAME)
        if not key_exists and db_exists:
            raise MaterialReceiptError("receipt_key_missing")
        if not key_exists:
            self._bootstrap_key()
        fd = self._open_regular(KEY_NAME)
        try:
            raw = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(raw) != 32:
            raise MaterialReceiptError("receipt_key_invalid")
        self._aes = AESGCM(raw)

    def _bootstrap_key(self):
        """generate -> private no-follow staging file -> fsync -> link to final name -> fsync dir."""
        key = AESGCM.generate_key(bit_length=256)
        staging = f".{KEY_NAME}.{secrets.token_hex(8)}.tmp"
        dirfd = self._dirfd()
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dirfd)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(key)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(staging, KEY_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
        finally:
            try:
                os.unlink(staging, dir_fd=dirfd)
            except FileNotFoundError:
                pass
        os.fsync(dirfd)

    # -- database -----------------------------------------------------------------------------
    def _init_db(self):
        fd = self._open_regular(DB_NAME, create=True)
        try:
            st = os.fstat(fd)
            self._db_id = (st.st_dev, st.st_ino)
        finally:
            os.close(fd)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (scope_key TEXT PRIMARY KEY, generation INTEGER NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, digest BLOB NOT NULL)")

    def _same_database(self, fd=None) -> bool:
        try:
            st = os.fstat(fd) if fd is not None else os.stat(DB_NAME, dir_fd=self._dirfd(), follow_symlinks=False)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == self._db_id and self._root_unchanged()

    @contextmanager
    def _db(self):
        fd = self._open_regular(DB_NAME)  # no-follow, regular, owner, 0600
        try:
            if not self._same_database(fd):
                _refuse()
            db = sqlite3.connect(str(self.path), isolation_level=None, timeout=5)
            try:
                if not self._same_database():
                    _refuse()
                db.execute("PRAGMA synchronous=FULL"); db.execute("PRAGMA fullfsync=1")
                yield db
            finally:
                db.close()
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

    def admit(self, scope, material: dict):
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
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT generation, nonce, ciphertext, digest FROM receipts WHERE scope_key=?", (key,)).fetchone()
            if row:
                if row[0] != scope.pairing_generation: raise MaterialReceiptError("material_scope_conflict")
                try:
                    existing = self._aes.decrypt(row[1], row[2], self._aad(scope))
                except Exception: raise MaterialReceiptError("material_unavailable") from None
                if hashlib.sha256(existing).digest() != row[3] or existing != plaintext: raise MaterialReceiptError("material_scope_conflict")
                return
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (key, scope.pairing_generation, nonce, encrypted, digest)); db.commit()

    def load(self, scope) -> dict:
        key = self.key_for(scope)
        with self._db() as db: row = db.execute("SELECT generation, nonce, ciphertext, digest FROM receipts WHERE scope_key=?", (key,)).fetchone()
        if row is None: raise MaterialReceiptError("material_scope_unapproved")
        if row[0] != scope.pairing_generation: raise MaterialReceiptError("material_scope_unapproved")
        try:
            raw = self._aes.decrypt(row[1], row[2], self._aad(scope))
            if hashlib.sha256(raw).digest() != row[3]: raise ValueError()
            value = json.loads(raw)
            _validate_material(value)  # defense in depth: never hand back a malformed record
            return value
        except Exception: raise MaterialReceiptError("material_unavailable") from None

    def admit_envelope(self, *, scope, envelope: dict, consent_public_key: bytes, expected_signed: dict, map_scope=None):
        """Verify the signed envelope exactly as received, then map/validate it against the local scope.

        Order: exact schema -> signature over the received bytes -> scope mapping -> grant -> catalog.
        Nothing is encrypted or inserted unless every step passes."""
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
        self.admit(scope, material)
