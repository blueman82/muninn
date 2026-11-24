"""Installer-private serialization of mutating installer entries."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from install.context import Ctx, StepFailedError
from muninn import file_sync, platform_io, platform_lock

OperationCheck = Callable[[], tuple[int, int]]


def _unsafe(info: os.stat_result) -> bool:
    """Report a lock file that is not a private single-link owner file."""
    return (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
    )


@contextmanager
def operation(ctx: Ctx) -> Generator[OperationCheck]:
    """Hold the private install lock for one mutating installer entry.

    A dry run takes no lock.

    Args:
        ctx: The run context.

    Yields:
        A check that refuses when the lock file was replaced.

    Raises:
        StepFailedError: ``install_root_unsafe`` if the release folder is
            not a private real folder of this user,
            ``install_lock_unsafe`` if the lock file is not private, not a
            plain file, or has more than one link, and
            ``install_lock_changed`` if the lock file changes.
    """
    if ctx.dry_run:
        yield lambda: (0, 0)
        return
    path = ctx.lib / ".install-record.lock"
    root = ctx.lib.lstat()
    if (
        not stat.S_ISDIR(root.st_mode)
        or root.st_uid != os.getuid()
        or root.st_mode & 0o077
        or ctx.lib.resolve(strict=True) != ctx.lib
    ):
        raise StepFailedError("install_root_unsafe")
    expected = path.lstat() if os.path.lexists(path) else None
    if expected is not None and (
        _unsafe(expected) or not platform_io.is_private(path)
    ):
        raise StepFailedError("install_lock_unsafe")
    fd = platform_lock.acquire(path)
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if expected and (expected.st_dev, expected.st_ino) != identity:
            raise StepFailedError("install_lock_changed")
        file_sync.sync_fd(fd)
        file_sync.sync_path(ctx.lib)
        check_lock(path, identity)
        yield lambda: check_lock(path, identity)
        check_lock(path, identity)
    finally:
        os.close(fd)


def check_lock(path: Path, identity: tuple[int, int]) -> tuple[int, int]:
    """Check that the lock path still names the locked file.

    Args:
        path: The lock file path.
        identity: The device and inode of the locked file.

    Returns:
        The unchanged identity.

    Raises:
        StepFailedError: ``install_lock_changed`` if the path now names
            another file, or the file is not private or not a plain
            single-link file.
    """
    found = path.lstat()
    if (
        _unsafe(found)
        or (found.st_dev, found.st_ino) != identity
        or not platform_io.is_private(path)
    ):
        raise StepFailedError("install_lock_changed")
    return identity
