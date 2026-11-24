"""Flush runtime file descriptors using the strongest available API."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


def _full_fsync(fd: int) -> bool:
    """Try macOS's drive-cache flush; report when fsync is still needed."""
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
    """Flush a file or directory without hiding a sync failure.

    Args:
        path: File or directory to flush.

    Raises:
        OSError: If the platform cannot flush the path.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        sync_fd(fd)
    finally:
        os.close(fd)
