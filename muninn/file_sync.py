"""Flush runtime file descriptors using each platform's strongest API."""

from __future__ import annotations

import os
import sys

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
