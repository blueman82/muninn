"""Retained private store copies and one-attempt replacement publication."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path
from typing import BinaryIO

from muninn import platform_io, platform_windows, store
from muninn.file_sync import sync_fd


def _close_all(resources: list[Callable[[], object]]) -> None:
    """Attempt every close, preserving the first close failure."""
    failure: BaseException | None = None
    for close in reversed(resources):
        try:
            close()
        except BaseException as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


@contextlib.contextmanager
def _resources() -> Generator[list[Callable[[], object]]]:
    """Close every descriptor without replacing the original failure."""
    resources: list[Callable[[], object]] = []
    try:
        yield resources
    except BaseException:
        with contextlib.suppress(BaseException):
            _close_all(resources)
        raise
    else:
        _close_all(resources)


def _private_info(path: Path, *, directory: bool = False) -> os.stat_result:
    """Require ordinary private owner state without repairing permissions."""
    info = path.lstat()
    if sys.platform == "win32":
        platform_windows.assert_private(path, directory=directory)
    else:
        ordinary = (
            stat.S_ISDIR(info.st_mode)
            if directory
            else stat.S_ISREG(info.st_mode)
        )
        if (
            not ordinary
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        ):
            raise PermissionError("store state is not private owner state")
    return info


def _paths(home: Path, *paths: Path) -> dict[Path, os.stat_result]:
    """Bind same-parent files beneath an existing unlinked private home."""
    for parent in (home, *home.parents):
        if parent.is_symlink():
            raise OSError("linked store ancestry is refused")
    found = {home: _private_info(home, directory=True)}
    for path in paths:
        if path.parent != home:
            raise OSError("store publication must stay in one directory")
        found[path] = _private_info(path)
    return found


def _unchanged(found: dict[Path, os.stat_result], home: Path) -> None:
    """Refuse changed paths before returning a copy or replacing a store."""
    for path, expected in found.items():
        actual = _private_info(path, directory=path == home)
        if (actual.st_dev, actual.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise OSError("store path changed identity")


def _read(
    source: Path,
    expected: os.stat_result,
    resources: list[Callable[[], object]],
) -> BinaryIO:
    """Retain a validated source descriptor without taking a database lock."""
    handle = platform_io.open_regular(
        source, root=source.parent, expected=expected
    )
    resources.append(handle.close)
    platform_io.assert_private_fd(handle.fileno())
    _unchanged({source: expected}, source.parent)
    return handle


def _sync_parent(home: Path) -> None:
    """Sync a POSIX directory without losing a sync error during close."""
    if sys.platform == "win32":
        raise OSError("Windows directory sync requires native publication")
    with _resources() as resources:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        resources.append(partial(os.close, fd))
        sync_fd(fd)


def retain_store(home: Path, source: Path) -> Path:
    """Copy a fenced source into a unique durable retained private archive.

    The caller holds actual maintenance and writer scopes, validates the
    current source, and closes database connections before copying. This
    operation neither acquires authority nor checks database contents.
    Failures leave every created artifact enumerable as recovery residue;
    only a returned path denotes a completed archive. Never restore it
    automatically or delete it as success or failure cleanup.

    Args:
        home: Existing private data directory with no linked ancestry.
        source: Current validated private regular source directly in home.

    Returns:
        The unique recovery pathname after copy and publication sync.

    Raises:
        OSError: If validation, exclusive creation, copy, close or sync fails.
            Original errors propagate, including FileExistsError collisions.
    """
    return _retain_store(home, source, store.RECOVERY_PREFIX)


def retain_upgrade_store(home: Path, source: Path) -> Path:
    """Copy a fenced upgrade source into a durable private retained archive.

    The caller holds actual maintenance and writer scopes, validates the
    source and closes SQLite connections before copying. No live source
    replacement, restoration, deletion or retry occurs. Complete and partial
    artifacts remain enumerable erasure residue; only a returned path means
    the archive completed copy, close and publication sync.

    Args:
        home: Existing private data directory with no linked ancestry.
        source: Validated private regular source directly inside home.

    Returns:
        Unique pathname beginning with the fixed pre-upgrade store prefix.

    Raises:
        OSError: If validation, exclusive creation, copy, close or sync fails.
            The original exception survives any subsequent cleanup failure.
    """
    return _retain_store(home, source, store.PRE_UPGRADE_PREFIX)


def _retain_store(home: Path, source: Path, prefix: str) -> Path:
    """Retain private bytes using one program-selected archive prefix."""
    found = _paths(home, source)
    archive = home / f"{prefix}{uuid.uuid4().hex}"
    temporary = (
        archive.with_name(f"{archive.name}.partial")
        if sys.platform == "win32"
        else archive
    )
    with _resources() as resources:
        input_file = _read(source, found[source], resources)
        fd = platform_io.open_private(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        resources.append(partial(os.close, fd))
        output = os.fdopen(fd, "wb", closefd=False)
        resources.append(output.close)
        platform_io.assert_private_fd(fd)
        shutil.copyfileobj(input_file, output)
        output.flush()
        sync_fd(fd)
        found[temporary] = os.fstat(fd)
        _unchanged(found, home)
    _unchanged(found, home)
    if sys.platform == "win32":
        platform_windows.publish(
            temporary, archive, replace=False, retry_move=False
        )
    else:
        _sync_parent(home)
    return archive


def publish_replacement(home: Path, db: Path, new: Path) -> None:
    """Attempt one replacement without restore, cleanup or retry on failure.

    The caller retains actual maintenance and writer scopes through this
    call, closes all database connections, and validates the current source,
    candidate, archive and latest erasure intent before calling. It owns
    admission, replay and all later uncertainty reporting. A failure after
    replacement is attempted never proves the old store remains live.
    Preserve live state and all archives on every outcome.

    Args:
        home: Existing private data directory with no linked ancestry.
        db: Existing private regular live database directly in home.
        new: Validated private regular candidate in home, distinct from db.

    Raises:
        OSError: If paths, identity, privacy, file sync or publication fail.
            The original exception propagates unchanged; possible exposure
            must be preserved even if the replacement call itself raises.
    """
    found = _paths(home, db, new)
    if (found[db].st_dev, found[db].st_ino) == (
        found[new].st_dev,
        found[new].st_ino,
    ):
        raise OSError("replacement must be a distinct file")
    with _resources() as resources:
        fd = platform_io.open_private(new, os.O_RDWR)
        resources.append(partial(os.close, fd))
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (
            found[new].st_dev,
            found[new].st_ino,
        ):
            raise OSError("candidate changed identity")
        sync_fd(fd)
    _unchanged(found, home)
    if sys.platform == "win32":
        platform_windows.publish(new, db, replace=True, retry_move=False)
    else:
        new.replace(db)
        _sync_parent(home)
