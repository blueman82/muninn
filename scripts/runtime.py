"""Private local-runtime ownership helpers."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


def private_directory(path: Path, create: bool) -> None:
    """Require a user-owned 0700 directory before local IPC use."""
    if create:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise PermissionError("private runtime directory is not user-owned")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise PermissionError("private runtime directory must be mode 0700")


def acquire_lock(state_dir: Path) -> tuple[Path, int]:
    """Create an exclusive daemon lock or fail while a live owner exists."""
    lock_path = state_dir / "daemon.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise RuntimeError("provenance daemon already running")
    os.ftruncate(descriptor, 0)
    os.write(descriptor, str(os.getpid()).encode())
    return lock_path, descriptor


def release_lock(_lock_path: Path, descriptor: int) -> None:
    """Remove only the lock created by this daemon process."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    except OSError:
        return
