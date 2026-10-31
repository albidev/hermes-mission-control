"""Owner-private, encrypted admission receipts with process-safe publication."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import re
from urllib.parse import urlsplit
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from invite_rooms.consent import canonical
from invite_rooms import private_fs
from invite_rooms.material_validators import (SCOPE_KEYS, MaterialScopeValidationError,
                                               validate_scope_attributes, validate_scope_dict)

_RECEIPT_VERSION = "mc-admission-receipt-v1"
_FILE_NAME = "admission_receipts.json.enc"
_KEY_NAME = "admission_receipts.key"
_LOCK_NAME = "admission_receipts.lock"
_PENDING_NAME = "admission_pending.json.enc"
_MAX_RECORDS = 4096
_MAX_BLOB_BYTES = 8 * 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024
_LOCK_WAIT = 0.5
_MARKER = b"\x01"
_KEY_BOOTSTRAP = b"K"
_STAGE_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_RUN_ID = re.compile(r"run_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_STATUSES = frozenset({"started", "queued", "running", "waiting_for_approval", "stopping",
                       "completed", "failed", "cancelled", "interrupted"})
_VERIFIER_VERSION = "mc-target-http-verifier-v1"
_MISSING = object()
_SCOPE_KEYS = SCOPE_KEYS
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_MAX_DIR_ENTRIES = 4096
_ARTIFACT_SUFFIXES = (".tmp", ".commit", ".backup", ".guard")
_RETAINED = re.compile(r"\.(%s|%s)\.[0-9a-f]{32}\.backup\Z" % (re.escape(_FILE_NAME), re.escape(_PENDING_NAME)))
_DIGITS = re.compile(rb"[0-9]{1,20}\Z")
_REPLACE_PHASES = frozenset({"intent", "stage", "backup", "guard", "commit", "published", "cleaning", "rollback"})
_CREATE_PHASES = frozenset({"intent", "stage", "published", "rollback"})


class AdmissionReceiptError(ValueError):
    """Sanitized fail-closed admission receipt error."""


def _scope_dict(scope) -> dict:
    try:
        value = scope.as_dict()
    except AttributeError:
        raise AdmissionReceiptError("invalid_material_scope") from None
    if type(value) is not dict or not value:
        raise AdmissionReceiptError("invalid_material_scope")
    try:
        return json.loads(canonical(value))
    except (TypeError, ValueError):
        raise AdmissionReceiptError("invalid_material_scope") from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if not isinstance(count, int) or count <= 0:
            raise OSError("short write")
        view = view[count:]


def _check_dir(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AdmissionReceiptError("admission_storage_unavailable")


def _check_regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise AdmissionReceiptError("admission_storage_unavailable")
    return info


def _fingerprint(fd: int) -> tuple[int, int, int, int, str]:
    info = os.fstat(fd)
    if info.st_size > _MAX_BLOB_BYTES:
        raise AdmissionReceiptError("admission_storage_corrupt")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        chunk = os.pread(fd, min(64 * 1024, info.st_size - offset), offset)
        if not chunk:
            raise OSError("short read")
        digest.update(chunk)
        offset += len(chunk)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest.hexdigest())


def _same_fingerprint(left, right) -> bool:
    return tuple(left) == tuple(right)


def _same_content(left, right) -> bool:
    return (left[2], left[4]) == (right[2], right[4])


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise AdmissionReceiptError("admission_deadline_exceeded")


class _Publication:
    """One completed publication: the final fingerprint it installed and how to undo it."""
    __slots__ = ("final_fp", "undo")

    def __init__(self, final_fp, undo):
        self.final_fp = final_fp
        self.undo = undo


class AdmissionReceiptStore:
    """Owner-private encrypted receipt store with descriptor-relative IO."""

    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(root))
        self._root_abs = self.root
        self._fd: Optional[int] = None
        self._root_id = None
        self._aes: AESGCM
        self._lock = threading.RLock()
        self._journal_fd: Optional[int] = None
        try:
            anchor = private_fs.nearest_existing_parent(self.root)
            self._reject_symlinked_ancestors(anchor)
            self._fd = self._open_root_descriptor()
            _check_dir(self._fd)
            root_stat = os.fstat(self._fd)
            self._root_id = (root_stat.st_dev, root_stat.st_ino)
            with self._bootstrap_transaction() as lock_fd:
                marker = os.pread(lock_fd, 1, 0)
                if marker not in (b"", _KEY_BOOTSTRAP, _MARKER):
                    raise AdmissionReceiptError("admission_storage_corrupt")
                self._recover_staging(allow_key_recovery=marker == _KEY_BOOTSTRAP, lock_fd=lock_fd)
                if not marker:
                    # Publish a durable bootstrap intent before creating the key. A
                    # restart may recover only a producer staging link while this
                    # marker is present; arbitrary hardlinks remain refused.
                    os.pwrite(lock_fd, _KEY_BOOTSTRAP, 0)
                    os.fsync(lock_fd)
                self._aes = AESGCM(self._load_or_create_key(initialized=marker == _MARKER))
                if marker != _MARKER:
                    os.pwrite(lock_fd, _MARKER, 0)
                    os.fsync(lock_fd)
                    os.fsync(self._fd)
                self._check_root()
                # Existing ciphertext is validated at construction, before callers can mutate it.
                self._read()
                self._read_pending()
        except AdmissionReceiptError:
            self.close()
            raise
        except (OSError, ValueError):
            self.close()
            raise AdmissionReceiptError("admission_storage_unavailable") from None

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        self.close()

    @property
    def path(self) -> Path:
        return self.root / _FILE_NAME

    def _dirfd(self) -> int:
        if self._fd is None:
            raise AdmissionReceiptError("admission_storage_unavailable")
        return self._fd

    def _reject_symlinked_ancestors(self, anchor: Path) -> None:
        """Reject attacker-created aliases below the lexical temporary root."""
        anchor = Path(os.path.abspath(anchor))
        configured = [Path(os.path.abspath(os.environ.get("TMPDIR", "/tmp"))), Path("/var"), Path("/tmp"), Path.home()]
        start = next((candidate for candidate in configured
                      if candidate == anchor or candidate in anchor.parents), Path(anchor.anchor))
        try:
            relative = anchor.relative_to(start)
        except ValueError:
            start, relative = Path(anchor.anchor), anchor.relative_to(Path(anchor.anchor))
        current = start
        for part in relative.parts:
            current /= part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    raise AdmissionReceiptError("admission_storage_unavailable")
            except FileNotFoundError:
                break

    def _open_root_descriptor(self) -> int:
        """Open the lexical path from one retained directory descriptor.

        In particular, never reopen ``anchor.parent`` (or any other ancestor) by
        pathname after choosing the anchor.  Every component below that trust
        boundary is resolved with ``dir_fd`` and ``O_NOFOLLOW``.
        """
        resolved = Path(os.path.abspath(self.root))
        configured_anchor = os.environ.get("HERMES_HOME", "").strip()
        if configured_anchor:
            candidate = Path(os.path.abspath(os.path.expanduser(configured_anchor)))
            try:
                resolved.relative_to(candidate)
                anchor = candidate
            except ValueError:
                anchor = Path(os.path.abspath(private_fs.nearest_existing_parent(self.root)))
        else:
            # /tmp is world-writable and can never be the trust boundary. Prefer
            # the parent of the nearest existing component when it is private;
            # otherwise use that private component itself (TemporaryDirectory).
            nearest = Path(os.path.abspath(private_fs.nearest_existing_parent(self.root)))
            def private_dir(path: Path) -> bool:
                try:
                    info = os.lstat(path)
                except OSError:
                    return False
                return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) & 0o022 == 0)
            parent = nearest.parent
            anchor = parent if private_dir(parent) else nearest
            if not private_dir(anchor):
                raise AdmissionReceiptError("admission_storage_unavailable")
        try:
            parts = resolved.relative_to(anchor).parts
        except ValueError:
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        fd = None
        try:
            # Bind the pathname-selected trust boundary before opening it.  A
            # swap at this seam is detected by comparing the retained fd inode.
            anchor_stat = os.lstat(anchor)
            if (not stat.S_ISDIR(anchor_stat.st_mode) or anchor_stat.st_uid != os.getuid()
                    or stat.S_IMODE(anchor_stat.st_mode) & 0o022):
                raise AdmissionReceiptError("admission_storage_unavailable")
            # This is the sole pathname open.  The caller must supply an
            # owner-controlled, non-writable anchor; all descendants are opened
            # relative to this retained descriptor.
            fd = os.open(anchor, private_fs.DIR_FLAGS)
            opened_stat = os.fstat(fd)
            if ((opened_stat.st_dev, opened_stat.st_ino) != (anchor_stat.st_dev, anchor_stat.st_ino)
                    or opened_stat.st_uid != os.getuid()
                    or stat.S_IMODE(opened_stat.st_mode) & 0o022):
                raise AdmissionReceiptError("admission_storage_unavailable")
            private_fs.check_dir(fd, private=not parts)
            for index, name in enumerate(parts):
                created = False
                try:
                    next_fd = os.open(name, private_fs.DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    created = True
                    next_fd = os.open(name, private_fs.DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                if created:
                    os.fchmod(fd, 0o700)
                private_fs.check_dir(fd, private=index == len(parts) - 1)
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise AdmissionReceiptError("admission_storage_unavailable") from None

    def _check_root(self) -> None:
        try:
            info = os.lstat(self._root_abs)
        except OSError:
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._root_id
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise AdmissionReceiptError("admission_storage_unavailable")
        _check_dir(self._dirfd())

    def _exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._dirfd(), follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise AdmissionReceiptError("admission_storage_unavailable") from None

    def _open_regular(self, name: str, *, create: bool = False, writable: bool = False) -> int:
        flags = (os.O_RDWR if (create or writable) else os.O_RDONLY) | _NOFOLLOW | _CLOEXEC | _NONBLOCK
        fd = None
        try:
            if create:
                try:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._dirfd())
                    os.fchmod(fd, 0o600)
                except FileExistsError:
                    fd = None
            if fd is None:
                fd = os.open(name, flags, dir_fd=self._dirfd())
            _check_regular(fd)
            current = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, current & ~_NONBLOCK)
            return fd
        except AdmissionReceiptError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError:
            if fd is not None:
                os.close(fd)
            raise AdmissionReceiptError("admission_storage_unavailable") from None

    def _acquire_flock(self, fd: int, *, deadline=None) -> None:
        _check_deadline(deadline)
        end = time.monotonic() + _LOCK_WAIT if deadline is None else min(deadline, time.monotonic() + _LOCK_WAIT)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                _check_deadline(deadline)
                if time.monotonic() >= end:
                    raise AdmissionReceiptError("admission_storage_busy")
                time.sleep(0.01)
            except OSError:
                raise AdmissionReceiptError("admission_storage_unavailable") from None

    @contextlib.contextmanager
    def _bootstrap_transaction(self, *, deadline=None):
        _check_deadline(deadline)
        timeout = _LOCK_WAIT if deadline is None else min(_LOCK_WAIT, max(0, deadline-time.monotonic()))
        if not self._lock.acquire(timeout=timeout):
            raise AdmissionReceiptError("admission_storage_busy")
        fd = None
        try:
            fd = self._open_lock()
            self._acquire_flock(fd, deadline=deadline)
            self._journal_fd = fd
            yield fd
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                self._journal_fd = None
            self._lock.release()

    @contextlib.contextmanager
    def _transaction(self, *, deadline=None):
        with self._bootstrap_transaction(deadline=deadline) as lock_fd:
            self._check_root()
            _check_deadline(deadline)
            try:
                self._recover_staging(allow_key_recovery=False, lock_fd=lock_fd)
            except OSError:
                raise AdmissionReceiptError("admission_storage_unavailable") from None
            yield
            _check_deadline(deadline)
            self._check_root()

    def _load_or_create_key(self, *, initialized: bool) -> bytes:
        if self._exists(_KEY_NAME):
            fd = self._open_regular(_KEY_NAME)
            try:
                raw = os.read(fd, 33)
            finally:
                os.close(fd)
            if len(raw) != 32:
                raise AdmissionReceiptError("admission_storage_corrupt")
            return raw
        if initialized or self._exists(_FILE_NAME) or self._exists(_PENDING_NAME):
            raise AdmissionReceiptError("admission_storage_corrupt")
        raw = AESGCM.generate_key(bit_length=256)
        self._publish(_KEY_NAME, raw, replace=False)
        return raw

    def _identity_matches(self, name: str, fd: int) -> bool:
        try:
            named = os.stat(name, dir_fd=self._dirfd(), follow_symlinks=False)
        except OSError:
            return False
        current = os.fstat(fd)
        return (named.st_dev, named.st_ino) == (current.st_dev, current.st_ino)

    def _identity_matches_bytes(self, name: str, fd: int, data: bytes) -> bool:
        if not self._identity_matches(name, fd):
            return False
        info = os.fstat(fd)
        return info.st_size == len(data) and os.pread(fd, len(data), 0) == data

    # ------------------------------------------------------------------
    # Publication protocol
    #
    # The stable lock inode carries ONE journal record describing at most one
    # in-flight publication of one target.  Roles: S stage (new bytes), C commit
    # (hardlink of S), B independent backup copy of the old bytes, G guard
    # (hardlink of the old inode), F the final name.  What recovery may find:
    #
    #   phase      artifacts                       F                   recovery
    #   intent     S (possibly partial)            old                 drop S
    #   stage      S, B (possibly partial)         old                 drop S,B
    #   backup     S, B, G?                        old|mutated         F from B if needed, drop S,G (B kept unlisted)
    #   guard      S, B, G, C?                     old|mutated         same
    #   commit     S?, B, G, C?                    old|new|other       F restored from B (else G)
    #   published  S?, B, G                        new                 drop S,G,B
    #   cleaning   S?, B?, G?                      new                 drop S,G,B
    #   rollback   S?, C?, B?, G?                  any                 F restored from B (else G)
    #   (create-only publications have no B/G/C; rollback unlinks F)
    #
    # Normal topology is a graph of hardlinks: S and C share one inode, and so do
    # G and the old F; every other alias is refused.  A completed publication
    # clears the journal but RETAINS its backup B as one bounded prior generation
    # per target until the next publication of that target.  Rollback authority
    # therefore outlives every in-call effect, including the journal clear and
    # the caller's pending cleanup.  Recovery validates the whole graph (role,
    # name, token, phase, fingerprint, type, identity, link count) before its
    # first effect and never infers a bootstrap from an empty journal.
    # ------------------------------------------------------------------

    def _inspect(self, name: str, *, missing_ok: bool = False):
        """No-follow, non-blocking, bounded open; returns (fingerprint, nlink) or None when absent."""
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=self._dirfd())
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        except OSError:
            raise AdmissionReceiptError("admission_storage_corrupt") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_BLOB_BYTES):
                raise AdmissionReceiptError("admission_storage_corrupt")
            return _fingerprint(fd), info.st_nlink
        except OSError:
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        finally:
            os.close(fd)

    def _artifact_names(self) -> list[str]:
        names = []
        try:
            with os.scandir(self._dirfd()) as entries:
                for index, entry in enumerate(entries):
                    if index >= _MAX_DIR_ENTRIES:
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    if entry.name.startswith(".") and entry.name.endswith(_ARTIFACT_SUFFIXES):
                        names.append(entry.name)
        except OSError:
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        if len(names) > 16:
            raise AdmissionReceiptError("admission_storage_corrupt")
        return sorted(names)

    def _check_retained(self, names, *, journal_target=None) -> None:
        """Unlisted artifacts must be single, valid retained prior-generation backups."""
        targets = set()
        for item in names:
            match = _RETAINED.fullmatch(item)
            if match is None or match.group(1) in targets or match.group(1) == journal_target:
                raise AdmissionReceiptError("admission_storage_corrupt")
            targets.add(match.group(1))
            if self._inspect(item)[1] != 1:
                raise AdmissionReceiptError("admission_storage_corrupt")

    def _drop_retained(self, name: str) -> None:
        """Retire the previous generation of ``name`` before a new journal record exists."""
        for item in self._artifact_names():
            match = _RETAINED.fullmatch(item)
            if match is not None and match.group(1) == name:
                if self._inspect(item)[1] != 1:
                    raise AdmissionReceiptError("admission_storage_corrupt")
                os.unlink(item, dir_fd=self._dirfd())

    def _open_lock(self) -> int:
        if not self._exists(_LOCK_NAME):
            # A store that already holds state never gets a fresh empty journal.
            if (self._exists(_KEY_NAME) or self._exists(_FILE_NAME) or self._exists(_PENDING_NAME)
                    or self._artifact_names()):
                raise AdmissionReceiptError("admission_storage_corrupt")
            return self._open_regular(_LOCK_NAME, create=True, writable=True)
        return self._open_regular(_LOCK_NAME, writable=True)

    def _copy_snapshot(self, source: str, target: str, expected) -> tuple[int, int, int, int, str]:
        """Create an independent, durable snapshot; never use a hardlink as the backup."""
        source_fd = self._open_regular(source)
        target_fd = None
        try:
            source_info = os.fstat(source_fd)
            if source_info.st_size > _MAX_BLOB_BYTES:
                raise AdmissionReceiptError("admission_storage_corrupt")
            digest = hashlib.sha256()
            chunks = []
            offset = 0
            while offset < source_info.st_size:
                chunk = os.pread(source_fd, min(64 * 1024, source_info.st_size - offset), offset)
                if not chunk:
                    raise OSError("short read")
                chunks.append(chunk)
                digest.update(chunk)
                offset += len(chunk)
            if (source_info.st_size, digest.hexdigest()) != (expected[2], expected[4]):
                raise AdmissionReceiptError("admission_storage_unavailable")
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                                0o600, dir_fd=self._dirfd())
            _write_all(target_fd, b"".join(chunks))
            os.fsync(target_fd)
            os.close(target_fd)
            target_fd = None
            copied = self._inspect(target)[0]
            if not _same_content(copied, expected):
                raise AdmissionReceiptError("admission_storage_unavailable")
            return copied
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)

    @staticmethod
    def _parse_journal(journal: bytes) -> dict:
        parts = journal.split(b"|")
        if len(parts) not in (10, 16, 22) or parts[0] not in (_KEY_BOOTSTRAP, _MARKER):
            raise AdmissionReceiptError("admission_storage_corrupt")

        def fingerprint(fields) -> tuple:
            numbers = fields[:4]
            if not all(_DIGITS.fullmatch(item) for item in numbers):
                raise ValueError
            return tuple(int(item) for item in numbers) + (fields[4].decode("ascii"),)

        try:
            name, stage, commit = (item.decode("ascii") for item in parts[1:4])
            record = {"marker": parts[0], "name": name, "stage": stage, "commit": commit,
                      "stage_fp": fingerprint(parts[4:9]), "backup": None, "backup_fp": None,
                      "guard": None, "guard_fp": None}
            record["phase"] = parts[9].decode("ascii")
            if len(parts) >= 16:
                record["backup"] = parts[9].decode("ascii")
                record["backup_fp"] = fingerprint(parts[10:15])
                record["phase"] = parts[15].decode("ascii")
            if len(parts) == 22:
                record["guard"] = parts[15].decode("ascii")
                record["guard_fp"] = fingerprint(parts[16:21])
                record["phase"] = parts[21].decode("ascii")
        except (UnicodeDecodeError, ValueError, IndexError):
            raise AdmissionReceiptError("admission_storage_corrupt") from None
        token = stage[len("."): -len(".tmp")].rsplit(".", 1)[-1] if stage.startswith(".") and stage.endswith(".tmp") else ""
        record["token"] = token
        phase, name = record["phase"], record["name"]
        replace = record["backup"] is not None
        if (not _STAGE_TOKEN.fullmatch(token) or name not in {_KEY_NAME, _FILE_NAME, _PENDING_NAME}
                or stage != f".{name}.{token}.tmp" or commit != f".{name}.{token}.commit"
                or phase not in (_REPLACE_PHASES if replace else _CREATE_PHASES)
                or (replace and name == _KEY_NAME)):
            raise AdmissionReceiptError("admission_storage_corrupt")
        if replace and (record["backup"] != f".{name}.{token}.backup"
                        or (record["guard"] is not None and record["guard"] != f".{name}.{token}.guard")):
            raise AdmissionReceiptError("admission_storage_corrupt")
        for key in ("stage_fp", "backup_fp", "guard_fp"):
            value = record[key]
            if value is not None and (len(value) != 5 or not _FINGERPRINT.fullmatch(value[4])
                                      or value[2] > _MAX_BLOB_BYTES):
                raise AdmissionReceiptError("admission_storage_corrupt")
        return record

    def _recover_staging(self, *, allow_key_recovery: bool, lock_fd: int) -> None:
        """Validate the entire artifact graph, then (and only then) apply idempotent recovery."""
        dirfd = self._dirfd()
        raw = os.pread(lock_fd, 4097, 0)
        if len(raw) > 4096:
            raise AdmissionReceiptError("admission_storage_corrupt")
        journal = raw.rstrip(b" ")
        if raw and not journal:
            raise AdmissionReceiptError("admission_storage_corrupt")
        names = self._artifact_names()
        if not journal:
            # Never infer a fresh bootstrap from an empty journal beside existing state.
            if names or any(self._exists(item) for item in (_KEY_NAME, _FILE_NAME, _PENDING_NAME)):
                raise AdmissionReceiptError("admission_storage_corrupt")
            return
        if journal in (_KEY_BOOTSTRAP, _MARKER):
            self._check_retained(names)
            return
        rec = self._parse_journal(journal)
        name, stage, commit = rec["name"], rec["stage"], rec["commit"]
        backup, guard, phase = rec["backup"], rec["guard"], rec["phase"]
        stage_fp, backup_fp, guard_fp = rec["stage_fp"], rec["backup_fp"], rec["guard_fp"]
        if name == _KEY_NAME and not allow_key_recovery:
            raise AdmissionReceiptError("admission_storage_corrupt")
        replace = backup is not None
        listed = [item for item in (stage, commit, backup, guard) if item is not None]
        if len(set(names)) != len(names):
            raise AdmissionReceiptError("admission_storage_corrupt")
        self._check_retained([item for item in names if item not in listed], journal_target=name)

        # ---- read-only graph inspection ----
        found = {item: self._inspect(item, missing_ok=True) for item in (*listed, name)}
        present = {item: value for item, value in found.items() if value is not None}
        for item, (fp, nlink) in present.items():
            aliases = sum(1 for other, _ in present.values() if (other[0], other[1]) == (fp[0], fp[1]))
            if nlink != aliases:
                raise AdmissionReceiptError("admission_storage_corrupt")

        def exact(item, expected) -> bool:
            return found.get(item) is not None and _same_fingerprint(found[item][0], expected)

        def same_inode(item, expected) -> bool:
            return found.get(item) is not None and found[item][0][:2] == expected[:2]

        def same_content(item, expected) -> bool:
            return found.get(item) is not None and (found[item][0][2], found[item][0][4]) == (expected[2], expected[4])

        committed = phase in {"published", "cleaning"}
        backup_ok = backup is not None and found[backup] is not None and phase not in {"intent", "stage"}
        if found[stage] is not None and phase != "intent" and not exact(stage, stage_fp):
            if committed or not same_inode(stage, stage_fp):
                raise AdmissionReceiptError("admission_storage_corrupt")
        if found.get(commit) is not None:
            if (not replace or phase not in {"guard", "commit", "rollback"}
                    or not (exact(commit, stage_fp) or (same_inode(commit, stage_fp) and not committed))):
                raise AdmissionReceiptError("admission_storage_corrupt")
        if backup is not None and found[backup] is not None:
            if phase == "intent" or (phase != "stage" and not exact(backup, backup_fp)):
                raise AdmissionReceiptError("admission_storage_corrupt")
        if guard is not None and found[guard] is not None:
            if phase in {"intent", "stage"} or not (exact(guard, guard_fp) or (same_inode(guard, guard_fp) and backup_ok)):
                raise AdmissionReceiptError("admission_storage_corrupt")
        guard_good = guard is not None and exact(guard, guard_fp) if guard is not None else False

        actions = []
        if committed:
            if not exact(name, stage_fp):
                raise AdmissionReceiptError("admission_storage_corrupt")
        elif not replace:
            if found[name] is None:
                if name == _KEY_NAME and found[stage] is not None and phase == "stage" and exact(stage, stage_fp):
                    if not allow_key_recovery:
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    actions.append(("link", stage))
            elif phase == "rollback":
                if not same_inode(name, stage_fp):
                    raise AdmissionReceiptError("admission_storage_corrupt")
                actions.append(("unlink", name))
            elif not (phase == "stage" and exact(name, stage_fp)):
                raise AdmissionReceiptError("admission_storage_corrupt")
        else:
            old = guard_fp if guard_fp is not None else backup_fp
            authority = backup if backup_ok else guard if guard_good else None
            if phase in {"intent", "stage"}:
                if not exact(name, old):
                    raise AdmissionReceiptError("admission_storage_corrupt")
            elif found[name] is None or not same_content(name, old):
                if authority is None:
                    raise AdmissionReceiptError("admission_storage_corrupt")
                actions.append(("restore", authority))

        # ---- effects: only after the complete graph validated ----
        if not (phase == "intent" and not actions and all(found.get(item) is None for item in listed)):
            for action, source in actions:
                if action == "restore":
                    os.replace(source, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                elif action == "link":
                    os.link(source, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                else:
                    os.unlink(source, dir_fd=dirfd)
            for item in listed:
                if self._exists(item):
                    os.unlink(item, dir_fd=dirfd)
            if actions and actions[0][0] == "restore":
                restored = self._inspect(name)[0]
                if (restored[2], restored[4]) != ((guard_fp or backup_fp)[2], (guard_fp or backup_fp)[4]):
                    raise AdmissionReceiptError("admission_storage_corrupt")
            os.fsync(dirfd)
        marker = rec["marker"]
        os.pwrite(lock_fd, marker, 0)
        os.ftruncate(lock_fd, 1)
        os.fsync(lock_fd)

    def _publish(self, name: str, data: bytes, *, replace: bool, deadline=None) -> _Publication:
        _check_deadline(deadline)
        dirfd = self._dirfd()
        token = secrets.token_hex(16)
        stage = f".{name}.{token}.tmp"
        commit = f".{name}.{token}.commit"
        backup = guard = None
        marker = os.pread(self._journal_fd, 1, 0) if self._journal_fd is not None else _MARKER
        old_fp = backup_fp = stage_fp = None
        fd = None
        zero = (0, 0, 0, 0, "0" * 64)

        if not replace and self._exists(name):
            raise FileExistsError(name)
        if replace:
            old_fd = self._open_regular(name)
            try:
                old_fp = _fingerprint(old_fd)
            except OSError:
                raise AdmissionReceiptError("admission_storage_unavailable") from None
            finally:
                os.close(old_fd)
            # All witnesses share the publication token: role and target are bound
            # together and raw journal aliases are never cleanup authority.
            backup = f".{name}.{token}.backup"
            guard = f".{name}.{token}.guard"

        def journal(stage_value=zero, phase="intent"):
            if self._journal_fd is None:
                return
            fields = [marker.decode("latin1"), name, stage, commit, *(str(value) for value in stage_value)]
            if backup is not None:
                fields.extend([backup, *(str(value) for value in (backup_fp or old_fp or zero))])
                fields.extend([guard, *(str(value) for value in (old_fp or zero))])
            fields.append(phase)
            raw = "|".join(fields).encode("ascii")
            if len(raw) > 4096:
                raise AdmissionReceiptError("admission_capacity")
            # One pwrite always leaves a complete, parseable record (pad, never a torn tail).
            previous = os.pread(self._journal_fd, 4097, 0)
            payload = raw + b" " * max(0, len(previous) - len(raw))
            if os.pwrite(self._journal_fd, payload, 0) != len(payload):
                raise OSError("short journal write")
            if len(payload) != len(raw):
                os.ftruncate(self._journal_fd, len(raw))
            os.fsync(self._journal_fd)

        def clear():
            if self._journal_fd is not None:
                os.pwrite(self._journal_fd, marker, 0)
                os.ftruncate(self._journal_fd, 1)
                os.fsync(self._journal_fd)

        def verify(item, expected):
            if not _same_fingerprint(self._inspect(item)[0], expected):
                raise AdmissionReceiptError("admission_storage_unavailable")

        def restore(mover):
            """Make the pre-publication state authoritative again; the journal records intent first."""
            journal(stage_fp, "rollback")
            current = self._inspect(name, missing_ok=True)
            if replace:
                if current is None or (current[0][2], current[0][4]) != (old_fp[2], old_fp[4]):
                    source = None
                    for candidate, expected in ((backup, backup_fp), (guard, old_fp)):
                        if expected is None:
                            continue
                        seen = self._inspect(candidate, missing_ok=True)
                        if seen is not None and _same_fingerprint(seen[0], expected):
                            source = candidate
                            break
                    if source is None:
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    mover(source, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                    restored = self._inspect(name)[0]
                    if (restored[2], restored[4]) != (old_fp[2], old_fp[4]):
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    os.fsync(dirfd)
            elif current is not None:
                if current[0][:2] != stage_fp[:2]:
                    raise AdmissionReceiptError("admission_storage_corrupt")
                os.unlink(name, dir_fd=dirfd)
            roles = [(stage, stage_fp), (commit, stage_fp)]
            if replace:
                roles.append((guard, old_fp))
            for artifact, expected in roles:
                seen = self._inspect(artifact, missing_ok=True)
                if seen is None:
                    continue
                if seen[0][:2] != expected[:2]:
                    # This is the live producer's abort path, not restart
                    # recovery.  Stage and commit are transaction-owned names
                    # and may be removed here only when the complete wrong
                    # inode is still accounted for by those two role names.
                    # Recovery never takes this shortcut: an unknown graph is
                    # rejected before effects in _recover_staging().
                    if artifact not in {stage, commit}:
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    aliases = []
                    for role in (stage, commit):
                        role_seen = self._inspect(role, missing_ok=True)
                        if role_seen is not None and role_seen[0][:2] == seen[0][:2]:
                            aliases.append(role)
                    if seen[1] != len(aliases):
                        raise AdmissionReceiptError("admission_storage_corrupt")
                    os.unlink(artifact, dir_fd=dirfd)
                    continue
                os.unlink(artifact, dir_fd=dirfd)
            if replace and backup_fp is None and self._exists(backup):
                os.unlink(backup, dir_fd=dirfd)  # incomplete copy; never a retained generation
            os.fsync(dirfd)
            clear()

        try:
            # The previous generation is retired before any journal record exists.
            self._drop_retained(name)
            # Retiring the bounded prior generation is an admitted maintenance
            # effect.  Do not start a new publication after that effect if the
            # caller's acknowledgement window has already closed.
            _check_deadline(deadline)
            journal()
            # The intent record is complete before opening the producer stage;
            # this gate prevents stage creation from becoming a late effect.
            _check_deadline(deadline)
            fd = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dirfd)
            _check_regular(fd)
            _write_all(fd, data)
            stage_fp = _fingerprint(fd)
            if not self._identity_matches_bytes(stage, fd, data):
                raise AdmissionReceiptError("admission_storage_unavailable")
            journal(stage_fp, "stage")
            os.fsync(fd)
            # Once stage production has started, its witness and fsync are
            # completed atomically from the protocol's point of view.  Only
            # after that witness may expiry stop the next phase.
            _check_deadline(deadline)
            if replace:
                _check_deadline(deadline)
                try:
                    backup_fp = self._copy_snapshot(name, backup, old_fp)
                    journal(stage_fp, "backup")
                    _check_deadline(deadline)
                    os.link(name, guard, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                    verify(guard, old_fp)
                    journal(stage_fp, "guard")
                    _check_deadline(deadline)
                    os.link(stage, commit, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                    verify(commit, stage_fp)
                    journal(stage_fp, "commit")
                except AdmissionReceiptError as exc:
                    restore(os.replace)
                    if str(exc) == "admission_deadline_exceeded":
                        raise
                    raise AdmissionReceiptError("admission_storage_unavailable") from None
                except (OSError, ValueError):
                    restore(os.replace)
                    raise AdmissionReceiptError("admission_storage_unavailable") from None
                # A failure of the replace itself leaves the journaled graph for recovery.
                try:
                    _check_deadline(deadline)
                except AdmissionReceiptError:
                    # No final visibility has started.  Restore the acknowledged
                    # generation now so the live store remains readable; the
                    # same witnessed graph is also valid for restart recovery.
                    restore(os.replace)
                    raise
                os.replace(commit, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
            else:
                _check_deadline(deadline)
                try:
                    os.link(stage, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                except (OSError, ValueError):
                    restore(os.replace)
                    raise AdmissionReceiptError("admission_storage_unavailable") from None
            try:
                verify(name, stage_fp)
            except AdmissionReceiptError:
                restore(os.rename)
                raise AdmissionReceiptError("admission_storage_unavailable") from None

            # From here every effect is followed by a check of the visible final;
            # the independent backup (or created-file identity) is kept until after
            # the last effect, and the backup is retained beyond the journal clear.
            deferred = False
            try:
                os.fsync(dirfd)
                verify(name, stage_fp)
                journal(stage_fp, "published")
                os.unlink(stage, dir_fd=dirfd)
                if self._exists(commit):
                    os.unlink(commit, dir_fd=dirfd)
                verify(name, stage_fp)
                if replace:
                    journal(stage_fp, "cleaning")
                os.fsync(dirfd)
                verify(name, stage_fp)
                if replace:
                    # The guard is a transient alias of the old inode.  If retiring it fails the
                    # committed receipt is still acknowledged; the cleaning journal stays as a
                    # bounded, replayable marker and a later transaction retires it.
                    try:
                        seen = self._inspect(guard, missing_ok=True)
                        if seen is not None:
                            if not _same_fingerprint(seen[0], old_fp):
                                raise AdmissionReceiptError("admission_storage_corrupt")
                            os.unlink(guard, dir_fd=dirfd)
                    except (AdmissionReceiptError, OSError):
                        deferred = True
                    verify(name, stage_fp)
                if not deferred:
                    clear()
                    verify(name, stage_fp)
            except (AdmissionReceiptError, OSError, ValueError):
                restore(os.replace)
                raise AdmissionReceiptError("admission_storage_unavailable") from None
            return _Publication(stage_fp, lambda: restore(os.replace))
        except FileExistsError:
            raise
        except AdmissionReceiptError:
            raise
        except (OSError, ValueError):
            raise AdmissionReceiptError("admission_storage_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _read_raw(self, name: str) -> bytes:
        fd = self._open_regular(name)
        try:
            chunks = []
            total = 0
            while total <= _MAX_BLOB_BYTES:
                chunk = os.read(fd, min(64 * 1024, _MAX_BLOB_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _decode_raw(self, raw: bytes, name: str, expected: set[str]) -> list[dict]:
        if len(raw) > _MAX_BLOB_BYTES or len(raw) < 12:
            raise AdmissionReceiptError("admission_storage_corrupt")
        try:
            value = json.loads(self._aes.decrypt(raw[:12], raw[12:], None))
            if not isinstance(value, list) or len(value) > _MAX_RECORDS:
                raise ValueError
            for item in value:
                if not isinstance(item, dict) or set(item) != expected:
                    raise ValueError
                self._validate_record(item, pending=name == _PENDING_NAME)
                encoded = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(encoded) > _MAX_RECORD_BYTES:
                    raise ValueError
            return value
        except Exception:
            raise AdmissionReceiptError("admission_storage_corrupt") from None

    def _decode(self, name: str, expected: set[str]) -> list[dict]:
        if not self._exists(name):
            return []
        return self._decode_raw(self._read_raw(name), name, expected)

    def _read(self) -> list[dict]:
        return self._decode(_FILE_NAME, {"version", "scope", "generation", "profile", "base_url",
                                         "idempotency_key", "request_fingerprint", "run_id", "status", "replayed"})

    def _read_pending(self) -> list[dict]:
        return self._decode(_PENDING_NAME, {"scope", "generation", "profile", "base_url",
                                             "idempotency_key", "request_fingerprint"})

    def _encode(self, records: list[dict]) -> bytes:
        if len(records) > _MAX_RECORDS:
            raise AdmissionReceiptError("admission_capacity")
        encoded_records = []
        for item in records:
            raw_item = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(raw_item) > _MAX_RECORD_BYTES:
                raise AdmissionReceiptError("admission_capacity")
            encoded_records.append(item)
        plaintext = json.dumps(encoded_records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(plaintext) > _MAX_BLOB_BYTES - 28:
            raise AdmissionReceiptError("admission_capacity")
        nonce = secrets.token_bytes(12)
        return nonce + self._aes.encrypt(nonce, plaintext, None)

    def _write_blob(self, name: str, records: list[dict], *, deadline=None) -> _Publication:
        _check_deadline(deadline)
        return self._publish(name, self._encode(records), replace=self._exists(name), deadline=deadline)

    @staticmethod
    def _key(record: dict) -> str:
        return hashlib.sha256(canonical({"scope": record["scope"], "generation": record["generation"],
                                         "profile": record["profile"], "base_url": record["base_url"],
                                         "idempotency_key": record["idempotency_key"]})).hexdigest()

    def reserve(self, *, scope, generation: int, profile: str, base_url: str,
                idempotency_key: str, request_fingerprint: str, deadline=None) -> Optional[dict]:
        self._validate_fields(generation=generation, profile=profile, base_url=base_url,
                              idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)
        scope_value = self._validated_scope(scope, generation=generation, profile=profile)
        candidate = {"scope": scope_value, "generation": generation, "profile": profile, "base_url": base_url,
                     "idempotency_key": idempotency_key, "request_fingerprint": request_fingerprint}
        with self._transaction(deadline=deadline):
            for record in self._read():
                if record["idempotency_key"] == idempotency_key:
                    if (record["scope"] != scope_value or record["generation"] != generation or
                            record["profile"] != profile or record["base_url"] != base_url or
                            record["request_fingerprint"] != request_fingerprint):
                        raise AdmissionReceiptError("idempotency_binding_mismatch")
                    return dict(record)
            for record in self._read_pending():
                if record["idempotency_key"] == idempotency_key:
                    if record != candidate:
                        raise AdmissionReceiptError("idempotency_binding_mismatch")
                    return None
            _check_deadline(deadline)
            self._write_blob(_PENDING_NAME, self._read_pending() + [candidate], deadline=deadline)
        return None

    def put(self, *, scope, generation: int, profile: str, base_url: str, idempotency_key: str,
            request_fingerprint: str, run_id: str, status: str, replayed: bool,
            verifier_version: str = _RECEIPT_VERSION, deadline=None) -> dict:
        self._validate_fields(generation=generation, profile=profile, base_url=base_url,
                              idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
                              run_id=run_id, status=status, verifier_version=verifier_version)
        if type(replayed) is not bool:
            raise AdmissionReceiptError("invalid_admission_receipt")
        scope_value = self._validated_scope(scope, generation=generation, profile=profile)
        record = {"version": verifier_version, "scope": scope_value, "generation": generation,
                  "profile": profile, "base_url": base_url, "idempotency_key": idempotency_key,
                  "request_fingerprint": request_fingerprint, "run_id": run_id, "status": status,
                  "replayed": replayed}
        if len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()) > _MAX_RECORD_BYTES:
            raise AdmissionReceiptError("admission_capacity")
        key = self._key(record)
        with self._transaction(deadline=deadline):
            records = self._read()
            for existing in records:
                if self._key(existing) == key:
                    if existing != record:
                        raise AdmissionReceiptError("admission_receipt_conflict")
                    self._retire_pending(records, _Publication(self._inspect(_FILE_NAME)[0], None))
                    return dict(existing)
            if len(records) >= _MAX_RECORDS:
                raise AdmissionReceiptError("admission_capacity")
            publication = self._write(records + [record], deadline=deadline)
            self._retire_pending(records + [record], publication, deadline=deadline)
        return dict(record)

    def _write(self, records: list[dict], *, deadline=None) -> _Publication:
        return self._write_blob(_FILE_NAME, records, deadline=deadline)

    def _retire_pending(self, records: list[dict], publication: _Publication, *, deadline=None) -> None:
        """Retire reservations covered by durable receipts inside the acknowledgement boundary.

        Retiring stays best effort (a failure leaves the bounded retry marker), but no effect
        here may leave the receipt file different from the one being acknowledged: that is
        re-verified last, and a mismatch rolls back the receipts AND the pending file."""
        pending_raw = None
        if self._exists(_PENDING_NAME):
            raw = self._read_raw(_PENDING_NAME)
            pending = self._decode_raw(raw, _PENDING_NAME, {"scope", "generation", "profile", "base_url",
                                                            "idempotency_key", "request_fingerprint"})
            committed = {record["idempotency_key"] for record in records}
            retained = [item for item in pending if item["idempotency_key"] not in committed]
            if retained != pending:
                pending_raw = raw
                try:
                    # Expiry before maintenance begins: start no new pending effect (pending may
                    # remain); the integrity check and rollback below still run.
                    _check_deadline(deadline)
                    if not retained:
                        os.unlink(_PENDING_NAME, dir_fd=self._dirfd())
                        os.fsync(self._dirfd())
                    elif self._journal_fd is None or len(os.pread(self._journal_fd, 2, 0)) <= 1:
                        # Rewriting needs the journal; a deferred cleaning record keeps it busy.
                        self._write_blob(_PENDING_NAME, retained, deadline=deadline)
                except (AdmissionReceiptError, OSError):
                    pass
        try:
            intact = _same_fingerprint(self._inspect(_FILE_NAME)[0], publication.final_fp)
        except AdmissionReceiptError:
            intact = False
        if intact:
            return
        if publication.undo is not None:
            publication.undo()
        if pending_raw is not None:
            self._restore_pending(pending_raw)
        raise AdmissionReceiptError("admission_storage_unavailable" if publication.undo is not None
                                    else "admission_storage_corrupt")

    def _restore_pending(self, raw: bytes) -> None:
        if self._exists(_PENDING_NAME):
            if self._read_raw(_PENDING_NAME) != raw:
                self._publish(_PENDING_NAME, raw, replace=True)
        else:
            self._publish(_PENDING_NAME, raw, replace=False)

    @staticmethod
    def _validate_fields(*, generation, profile, base_url, idempotency_key, request_fingerprint,
                         run_id=_MISSING, status=_MISSING, verifier_version=_MISSING) -> None:
        if (type(generation) is not int or generation < 1 or type(profile) is not str
                or not _PROFILE.fullmatch(profile) or type(base_url) is not str
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in base_url)
                or type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 255
                or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key)
                or type(request_fingerprint) is not str or not _FINGERPRINT.fullmatch(request_fingerprint)):
            raise AdmissionReceiptError("invalid_admission_receipt")
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                    or parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.path
                    or parsed.query or parsed.fragment or base_url.endswith(("?", "#"))
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError
        except (ValueError, TypeError):
            raise AdmissionReceiptError("invalid_admission_receipt") from None
        if run_id is not _MISSING and (type(run_id) is not str or not _RUN_ID.fullmatch(run_id)):
            raise AdmissionReceiptError("invalid_admission_receipt")
        if status is not _MISSING and (type(status) is not str or status not in _STATUSES):
            raise AdmissionReceiptError("invalid_admission_receipt")
        if (verifier_version is not _MISSING
                and (type(verifier_version) is not str
                     or verifier_version not in {_RECEIPT_VERSION, _VERIFIER_VERSION})):
            raise AdmissionReceiptError("invalid_admission_receipt")

    @classmethod
    def _validated_scope(cls, scope, *, generation: int, profile: str) -> dict:
        try:
            return validate_scope_attributes(scope, generation=generation, profile=profile)
        except MaterialScopeValidationError:
            raise AdmissionReceiptError("invalid_material_scope") from None

    @classmethod
    def _validate_record(cls, record: dict, *, pending: bool) -> None:
        try:
            scope = record["scope"]
            generation = record["generation"]
            profile = record["profile"]
            validate_scope_dict(scope)
            cls._validate_fields(
                generation=generation, profile=profile, base_url=record["base_url"],
                idempotency_key=record["idempotency_key"],
                request_fingerprint=record["request_fingerprint"],
                **({} if pending else {
                    "run_id": record["run_id"],
                    "status": record["status"],
                    "verifier_version": record["version"],
                }),
            )
            if scope["pairing_generation"] != generation or scope["target_profile"] != profile:
                raise ValueError
            if pending:
                return
            if (type(record["version"]) is not str
                    or record["version"] not in {_RECEIPT_VERSION, _VERIFIER_VERSION}
                    or type(record["run_id"]) is not str or not _RUN_ID.fullmatch(record["run_id"])
                    or type(record["status"]) is not str or record["status"] not in _STATUSES
                    or type(record["replayed"]) is not bool):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AdmissionReceiptError("admission_storage_corrupt") from None

    def find(self, *, scope, generation: int, profile: str, base_url: str,
             idempotency_key: str, request_fingerprint: str, deadline=None) -> Optional[dict]:
        self._validate_fields(generation=generation, profile=profile, base_url=base_url,
                              idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)
        scope_value = self._validated_scope(scope, generation=generation, profile=profile)
        with self._transaction(deadline=deadline):
            records = self._read()
        for record in records:
            if (record.get("scope") == scope_value and record.get("generation") == generation
                    and record.get("profile") == profile and record.get("base_url") == base_url
                    and record.get("idempotency_key") == idempotency_key
                    and record.get("request_fingerprint") == request_fingerprint):
                return dict(record)
        return None

    def find_idempotency_key(self, idempotency_key: str) -> Optional[dict]:
        if type(idempotency_key) is not str:
            raise AdmissionReceiptError("invalid_idempotency_key")
        with self._transaction():
            records = self._read()
        return next((dict(r) for r in records if r.get("idempotency_key") == idempotency_key), None)

    def count(self) -> int:
        with self._transaction():
            return len(self._read())
