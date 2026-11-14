"""Descriptor-relative, no-follow private-storage primitives (P3b.1a-1).

Same checks as the receipt store (``material_receipts``) but generic and with its own error type; the receipt
store is deliberately NOT changed to use this module (its behaviour and suite stay byte-identical).

Every directory is opened ``O_DIRECTORY|O_NOFOLLOW`` relative to its parent descriptor and fstat-checked
(type, owner UID, mode).  Files are opened no-follow relative to a directory descriptor and fstat-checked
(regular, owner UID, exactly 0600, single link).  Nothing is ever repaired or chmod-ed on a pre-existing object.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import NoReturn

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC


class PrivateStorageError(ValueError):
    """Sanitized: the message is always ``private_storage_required``."""

    def __init__(self):
        super().__init__("private_storage_required")


def refuse() -> NoReturn:
    raise PrivateStorageError()


def check_dir(fd: int, *, private: bool) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        refuse()
    mode = stat.S_IMODE(st.st_mode)
    if (private and mode != 0o700) or (not private and mode & 0o022):
        refuse()


def check_regular(fd: int) -> None:
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1):
        refuse()


def nearest_existing_parent(root: Path) -> Path:
    candidate = Path(os.path.abspath(root)).parent
    while not os.path.lexists(candidate) and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def open_private_dir(root: Path, anchor: Path | None = None, *, create: bool) -> int:
    """Directory fd for ``root`` reached from ``anchor`` without following links.

    The anchor must be owner-controlled and not group/world-writable; components below it are created 0700 only
    when ``create`` is true; the final component must be exactly 0700.  Ancestors ABOVE the anchor are not our
    business (``/var`` -> ``/private/var``)."""
    root_abs = Path(os.path.abspath(root))
    anchor_abs = Path(os.path.abspath(anchor)) if anchor is not None else nearest_existing_parent(root_abs)
    try:
        parts = root_abs.relative_to(anchor_abs).parts
    except ValueError:
        refuse()
    try:
        fd = os.open(anchor_abs, DIR_FLAGS)
    except OSError:
        refuse()
    try:
        check_dir(fd, private=not parts)
        for index, name in enumerate(parts):
            created = False
            try:
                nfd = os.open(name, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    refuse()
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                nfd = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if created:
                os.fchmod(fd, 0o700)
            check_dir(fd, private=index == len(parts) - 1)
        return fd
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, (PrivateStorageError, OSError)):
            refuse()
        raise


def open_regular(dirfd: int, name: str, *, create: bool = False, write: bool = False) -> int:
    """No-follow open relative to ``dirfd``; fstat regular/UID/0600/nlink==1 or refuse.  ``create`` is O_EXCL."""
    flags = (os.O_RDWR if (create or write) else os.O_RDONLY) | _NOFOLLOW | _CLOEXEC | _NONBLOCK  # a FIFO must not block us
    fd = None
    try:
        if create:
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
                os.fchmod(fd, 0o600)
            except FileExistsError:
                fd = None
        if fd is None:
            fd = os.open(name, flags, dir_fd=dirfd)
        check_regular(fd)
        return fd
    except BaseException as exc:
        if fd is not None:
            os.close(fd)
        if isinstance(exc, (PrivateStorageError, OSError)):
            refuse()
        raise


def read_all(fd: int, limit: int) -> bytes:
    data = b""
    while len(data) <= limit:
        chunk = os.read(fd, limit + 1 - len(data))
        if not chunk:
            return data
        data += chunk
    refuse()


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def fsync(fd: int) -> None:
    os.fsync(fd)
    try:  # macOS: push through the drive cache as well
        import fcntl
        fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))
    except (ImportError, OSError):
        pass
