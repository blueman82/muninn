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
    """Serialize mutating installer entries behind one private lock."""
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
    """Refuse a substituted lock pathname while retaining the original fd."""
    found = path.lstat()
    if (
        _unsafe(found)
        or (found.st_dev, found.st_ino) != identity
        or not platform_io.is_private(path)
    ):
        raise StepFailedError("install_lock_changed")
    return identity
