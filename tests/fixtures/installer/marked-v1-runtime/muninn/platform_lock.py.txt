"""One nonblocking writer lock using the native descriptor API."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

from muninn import platform_io

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def try_lock(fd: int) -> None:
    """Acquire an exclusive lock or raise on contention or an IO failure.

    Separate opens must conflict even within one process. Closing the
    descriptor releases the lock, including after a process crash.

    Args:
        fd: Open read-write descriptor retained until the lock is released.

    Raises:
        BlockingIOError: If another open descriptor holds the lock.
        OSError: If the descriptor or filesystem cannot support the lock.
    """
    if sys.platform != "win32":
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    # _locking starts at the current position and can lock beyond EOF.
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno != errno.EACCES:
            raise
        raise BlockingIOError(errno.EAGAIN, "writer lock is held") from exc


def acquire(path: Path) -> int:
    """Retain an exclusive noninheritable descriptor until explicit close."""
    fd = platform_io.open_private(path, os.O_CREAT | os.O_RDWR)
    try:
        os.set_inheritable(fd, False)
        try_lock(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def released(data: Path) -> bool:
    """Require writer and supervisor release after native job disappearance."""
    if not data.exists():
        return True
    descriptors: list[int] = []
    try:
        descriptors.extend(
            acquire(data / name)
            for name in ("review.supervisor.lock", "writer.lock")
        )
        return True
    except BlockingIOError:
        return False
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
