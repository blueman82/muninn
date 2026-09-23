"""Private local-runtime ownership helpers."""

from __future__ import annotations

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


def acquire_lock(state_dir: Path) -> Path:
    """Create an exclusive daemon lock or fail while a live owner exists."""
    lock_path = state_dir / "daemon.lock"
    try:
        descriptor = os.open(
            lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        try:
            owner = int(lock_path.read_text().strip())
            os.kill(owner, 0)
        except (OSError, ValueError):
            lock_path.unlink()
            return acquire_lock(state_dir)
        raise RuntimeError("provenance daemon already running")
    with os.fdopen(descriptor, "w") as stream:
        stream.write(str(os.getpid()))
    return lock_path


def release_lock(lock_path: Path) -> None:
    """Remove only the lock created by this daemon process."""
    try:
        if lock_path.read_text().strip() == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        return
