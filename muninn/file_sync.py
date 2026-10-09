"""Flush runtime file descriptors using each platform's strongest API."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform != "win32":
    import fcntl


def _full_fsync(fd: int) -> bool:
    """Try macOS's drive-cache flush; report when fsync is still needed."""
    if sys.platform != "darwin":
        return False
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except OSError:
        return False
    return True


def sync_fd(fd: int) -> None:
    """Flush an open file, propagating any failed fallback sync.

    Args:
        fd: Descriptor to flush; the caller owns and closes it.
    """
    if not _full_fsync(fd):
        os.fsync(fd)


def sync_path(path: Path) -> None:
    """Flush a POSIX file or directory without hiding a sync failure.

    Windows directory publication uses the native write-through move,
    because a descriptor fsync does not establish directory durability.

    Args:
        path: File or directory to flush.

    Raises:
        OSError: If the platform cannot flush the path.
    """
    if sys.platform == "win32" and path.is_dir():
        raise OSError("Windows directory sync requires native publication")
    flags = os.O_RDWR if sys.platform == "win32" else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        sync_fd(fd)
    finally:
        os.close(fd)
