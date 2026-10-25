"""Anchored checks around SQLite pathname opens (not a descriptor-backed VFS).

Ancestors above the anchor are trusted. Pre/post checks detect visible swaps,
not a same-uid ABA entirely inside SQLite's pathname open, nor races after the
checks. See the F4 threat model; do not claim receipt-store confinement here.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _identity(st):
    return st.st_dev, st.st_ino


class AnchoredSQLite:
    """One private directory, pinned DB/key identities and checked SQLite side files."""

    def __init__(self, root, db_name, key_name, error, *, anchor=None):
        self.error = error
        self.root = Path(os.path.abspath(root))
        self.db_name, self.key_name = db_name, key_name
        self.path = self.root / db_name
        self._fd = self._dbfd = None
        self._ids = {}
        if anchor is None:
            anchor = self.root.parent
            while not os.path.lexists(anchor) and anchor != anchor.parent:
                anchor = anchor.parent
        self.anchor = Path(os.path.abspath(anchor))
        try:
            self.parts = self.root.relative_to(self.anchor).parts
            if any(p in ('.', '..') for p in self.parts):
                self._refuse()
            self._fd = self._walk(create=True)
            self._root_id = _identity(os.fstat(self._fd))
        except (OSError, ValueError):
            self.close()
            self._refuse()

    def _refuse(self):
        raise self.error('private_storage_required') from None

    def _walk(self, *, create=False):
        fd = os.open(self.anchor, _DIR)
        try:
            self._check_dir(fd, private=not self.parts)
            for i, name in enumerate(self.parts):
                try:
                    nfd = os.open(name, _DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nfd = os.open(name, _DIR, dir_fd=fd)
                os.close(fd)
                fd = nfd
                self._check_dir(fd, private=i == len(self.parts) - 1)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_dir(self, fd, *, private):
        st = os.fstat(fd)
        mode = stat.S_IMODE(st.st_mode)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                or (mode != 0o700 if private else mode & 0o022)):
            self._refuse()

    def _regular(self, name, *, create=False, optional=False):
        fd = None
        try:
            flags = _FILE | (os.O_RDWR if create else os.O_RDONLY)
            if create:
                try:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._fd)
                    os.fchmod(fd, 0o600)
                except FileExistsError:
                    pass
            if fd is None:
                fd = os.open(name, flags, dir_fd=self._fd)
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
                self._refuse()
            named = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            if _identity(named) != _identity(st):
                self._refuse()
            if name in self._ids and self._ids[name] != _identity(st):
                self._refuse()
            return fd
        except FileNotFoundError:
            if fd is not None:
                os.close(fd)
            if optional:
                return None
            self._refuse()
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def _check(self, *, require_key=True):
        try:
            if self._fd is None:
                self._refuse()
            self._check_dir(self._fd, private=True)
            fd = self._walk()
            try:
                if _identity(os.fstat(fd)) != self._root_id:
                    self._refuse()
            finally:
                os.close(fd)
            names = (self.db_name, 'initialize.lock') + ((self.key_name,) if require_key else ())
            for name in names:
                fd = self._regular(name)
                os.close(fd)
            if not require_key:
                fd = self._regular(self.key_name, optional=True)
                if fd is not None:
                    os.close(fd)
            self._check_side_files()
        except OSError:
            self._refuse()

    def _check_side_files(self):
        for suffix in ('-wal', '-shm', '-journal'):
            fd = self._regular(self.db_name + suffix, optional=True)
            if fd is not None:
                os.close(fd)

    @contextmanager
    def initialize(self, validate_existing):
        """Bounded cross-process bootstrap lock; never regenerate a lost DB key."""
        fd = None
        try:
            fd = self._regular('initialize.lock', create=True)
            self._ids['initialize.lock'] = _identity(os.fstat(fd))
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self._refuse()
                    time.sleep(0.005)
            self._check_side_files()
            try:
                os.stat(self.db_name, dir_fd=self._fd, follow_symlinks=False)
                fresh = False
            except FileNotFoundError:
                fresh = True
            self._dbfd = self._regular(self.db_name, create=fresh)
            self._ids[self.db_name] = _identity(os.fstat(self._dbfd))
            if not fresh:
                # Diagnose foreign/legacy layouts before requiring a key, without
                # creating one or changing journal mode on an unrecognized DB.
                with self.connection(require_key=False) as conn:
                    validate_existing(conn)
            if fresh:
                # O_EXCL prevents adopting/replacing a pre-existing unsafe key.
                try:
                    keyfd = os.open(self.key_name, _FILE | os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                    0o600, dir_fd=self._fd)
                except FileExistsError:
                    keyfd = None
                if keyfd is not None:
                    try:
                        os.fchmod(keyfd, 0o600)
                        if os.write(keyfd, AESGCM.generate_key(bit_length=256)) != 32:
                            self._refuse()
                        os.fsync(keyfd)
                        os.fsync(self._fd)
                    finally:
                        os.close(keyfd)
            keyfd = self._regular(self.key_name)
            try:
                self._ids[self.key_name] = _identity(os.fstat(keyfd))
                key = os.read(keyfd, 33)
            finally:
                os.close(keyfd)
            if len(key) != 32:
                self._refuse()
            self._check()
            yield fresh, AESGCM(key)
        except (OSError, sqlite3.DatabaseError):
            self._refuse()
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def connection(self, *, timeout=10, require_key=True):
        conn = None
        try:
            self._check(require_key=require_key)  # pre-connect
            uri = 'file:' + quote(str(self.path), safe='/') + '?mode=rw'
            conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
            self._check(require_key=require_key)  # post-connect: BEFORE any SQL or row read
            yield conn
        except (OSError, sqlite3.DatabaseError):
            self._refuse()
        finally:
            if conn is not None:
                conn.close()

    def close(self):
        for name in ('_dbfd', '_fd'):
            fd = getattr(self, name, None)
            setattr(self, name, None)
            if fd is not None:
                os.close(fd)

    def __del__(self):
        self.close()
