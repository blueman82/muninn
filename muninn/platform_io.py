"""Private state files and safe binary source reads on supported platforms."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO

from muninn import file_sync


def ensure_private_dir(path: Path) -> None:
    """Create a private leaf directory and force mode 0700 on it."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def open_private(path: Path, flags: int) -> int:
    """Open regular private state without writing before permission checks.

    On POSIX, preserve an effective-user-owned exact-0600 descriptor when
    no BSD flags are observable. Other modes, owners and flagged or unknown
    BSD flag values retain permission normalization through fchmod.

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
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            raise OSError("state is not a regular file")
        bsd_flags: object = getattr(found, "st_flags", 0)
        if (
            stat.S_IMODE(found.st_mode) != 0o600
            or found.st_uid != os.geteuid()
            or type(bsd_flags) is not int
            or bsd_flags != 0
        ):
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
    """Check the owner-only mode.

    Args:
        path: State object to inspect.
        directory: Require mode 0700 rather than owner-only file bits.

    Returns:
        Whether access control can be established as private.
    """
    try:
        bits = path.stat().st_mode & 0o777
        return bits == 0o700 if directory else bits & 0o077 == 0
    except OSError:
        return False


def publish_private_file(path: Path, raw: bytes) -> bool:
    """Publish complete immutable private bytes without replacing a prior file.

    Args:
        path: Final leaf inside an already checked private directory.
        raw: Bounded metadata bytes already validated by the caller.

    Returns:
        Whether this caller published the file rather than finding a prior one.
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            file_sync.sync_fd(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        file_sync.sync_path(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)
