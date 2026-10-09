"""Private state files and safe binary source reads on supported platforms."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import BinaryIO

from muninn import platform_windows


def ensure_private_dir(path: Path) -> None:
    """Create a private leaf directory or validate an existing Windows ACL."""
    if sys.platform == "win32":
        platform_windows.assert_ancestry(path)
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            platform_windows.assert_private(directory, directory=True)
        platform_windows.assert_private(path, directory=True)
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)


def open_private(path: Path, flags: int) -> int:
    """Open regular private state without writing before permission checks.

    Args:
        path: State file beneath a private data directory.
        flags: os.open flags; do not include O_TRUNC before validation.

    Returns:
        The caller-owned binary descriptor.

    Raises:
        OSError: If the path or access control cannot be validated.
        ValueError: If truncation would precede privacy validation.
    """
    if flags & os.O_TRUNC:
        raise ValueError("validate private state before truncating it")
    if sys.platform == "win32":
        return platform_windows.open_private(path, flags)
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("state is not a regular file")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_regular(
    path: Path,
    *,
    root: Path | None = None,
    expected: os.stat_result | None = None,
) -> BinaryIO:
    """Open an intended regular source without links or byte conversion.

    Args:
        path: Candidate source file.
        root: Optional boundary the source must remain under.
        expected: Original discovery stat; a different identity is refused.

    Returns:
        A caller-owned binary stream.

    Raises:
        OSError: If the source is unsafe or changed identity.
    """
    if root is not None:
        base, actual = root.resolve(), path.resolve()
        if not actual.is_relative_to(base):
            raise OSError("source is outside its root")
    if sys.platform == "win32":
        fd = platform_windows.open_regular(path, root)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        actual_stat = os.fstat(fd)
        if not stat.S_ISREG(actual_stat.st_mode):
            raise OSError("source is not a regular file")
        if expected is not None and (
            actual_stat.st_dev,
            actual_stat.st_ino,
        ) != (expected.st_dev, expected.st_ino):
            raise OSError("source changed identity")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def is_private(path: Path, *, directory: bool = False) -> bool:
    """Check Windows ACLs or the equivalent POSIX owner-only mode.

    Args:
        path: State object to inspect.
        directory: Require mode 0700 rather than owner-only file bits.

    Returns:
        Whether access control can be established as private.
    """
    try:
        if sys.platform == "win32":
            platform_windows.assert_private(path, directory=directory)
            return True
        bits = path.stat().st_mode & 0o777
        return bits == 0o700 if directory else bits & 0o077 == 0
    except OSError:
        return False


def assert_private_fd(fd: int) -> None:
    """Check an opened Windows temp file before private writes."""
    if sys.platform == "win32":
        platform_windows.private_fd(fd)
