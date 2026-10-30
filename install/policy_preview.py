"""Read a stable supported policy store without provisioning or repair."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager, nullcontext, suppress
from pathlib import Path

from install import snapshot_io
from install.context import Ctx, StepFailedError
from install.policy_source_format import validate_policy_source
from install.review_policy_transition import compatible
from muninn import platform_io, platform_lock, platform_windows, store
from muninn.review_models import ReviewPolicy


def _metadata(value: os.stat_result) -> tuple[int, ...]:
    """Compare one native view against itself, including content metadata."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    """Validate private existing state without fixing its permissions."""
    found = path.lstat()
    ordinary = (
        stat.S_ISDIR(found.st_mode)
        if directory
        else stat.S_ISREG(found.st_mode)
    )
    if not ordinary or not platform_io.is_private(path, directory=directory):
        raise PermissionError("unsafe preview state")
    if sys.platform != "win32" and (
        found.st_uid != os.getuid()
        or stat.S_IMODE(found.st_mode) != (0o700 if directory else 0o600)
    ):
        raise PermissionError("unsafe preview owner or mode")
    if not directory and found.st_nlink != 1:
        raise PermissionError("linked preview state")
    return found


@contextmanager
def _ownership(home: Path) -> Generator[Callable[[], None]]:
    """Hold the existing writer inode using its native read-write contract."""
    directory = _private(home, directory=True)
    path = home / "writer.lock"
    before = _private(path)
    fd = (
        platform_windows.open_private(path, os.O_RDWR)
        if sys.platform == "win32"
        else os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    )
    try:
        os.set_inheritable(fd, False)
        platform_io.assert_private_fd(fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StepFailedError(
                "review_policy_preview_unavailable: lock_changed"
            )
        try:
            platform_lock.try_lock(fd)
        except BlockingIOError as exc:
            raise StepFailedError(
                "review_policy_preview_unavailable: writer_busy"
            ) from exc

        def check() -> None:
            if _metadata(_private(home, directory=True)) != _metadata(
                directory
            ):
                raise StepFailedError(
                    "review_policy_preview_unavailable: directory_changed"
                )
            if _metadata(_private(path)) != _metadata(before) or _metadata(
                os.fstat(fd)
            ) != _metadata(opened):
                raise StepFailedError(
                    "review_policy_preview_unavailable: lock_changed"
                )
            platform_io.assert_private_fd(fd)

        check()
        yield check
    finally:
        if sys.exception() is None:
            os.close(fd)
        else:
            with suppress(OSError):
                os.close(fd)


def _no_sidecars(path: Path) -> None:
    """Refuse pending recovery or WAL contents rather than ignoring them."""
    if any(
        os.path.lexists(f"{path}{suffix}")
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise StepFailedError(
            "review_policy_preview_unavailable: sidecar_present"
        )


@contextmanager
def connection(path: Path) -> Generator[sqlite3.Connection]:
    """Read a private DELETE image under the cooperating writer protocol.

    The existing writer lock stays held until SQLite closes. Unsupported
    recovery state is refused rather than healed, copied or read as stale data.

    Args:
        path: Existing store in its private data directory.

    Yields:
        A read-only connection held inside the validated writer ownership.

    Raises:
        StepFailedError: If a private stable supported image cannot be proved.
    """
    try:
        with (
            _ownership(path.parent) as ownership,
            snapshot_io.validated(path) as identity,
        ):
            before = _private(path)
            with platform_io.open_regular(
                path, root=path.parent, expected=before
            ) as handle:
                platform_io.assert_private_fd(handle.fileno())
                opened = os.fstat(handle.fileno())

                def check() -> None:
                    ownership()
                    identity()
                    _no_sidecars(path)
                    platform_io.assert_private_fd(handle.fileno())
                    if _metadata(_private(path)) != _metadata(
                        before
                    ) or _metadata(os.fstat(handle.fileno())) != _metadata(
                        opened
                    ):
                        raise StepFailedError(
                            "review_policy_preview_unavailable: "
                            "database_changed"
                        )

                check()
                header = handle.read(100)
                if (
                    len(header) != 100
                    or header[:16] != b"SQLite format 3\x00"
                    or header[18:20] != b"\x01\x01"
                ):
                    raise StepFailedError(
                        "review_policy_preview_unavailable: "
                        "unsupported_header"
                    )
                check()
                with closing(
                    sqlite3.connect(
                        path.absolute().as_uri() + "?mode=ro&immutable=1",
                        uri=True,
                    )
                ) as conn:
                    check()
                    yield conn
                    check()
                check()
    except FileNotFoundError as exc:
        raise StepFailedError(
            "review_policy_preview_unavailable: state_missing"
        ) from exc
    except PermissionError as exc:
        raise StepFailedError(
            "review_policy_preview_unavailable: unsafe_state"
        ) from exc
    except sqlite3.Error as exc:
        raise StepFailedError(
            "review_policy_preview_unavailable: sqlite_error"
        ) from exc
    except OSError as exc:
        raise StepFailedError(
            "review_policy_preview_unavailable: io_error"
        ) from exc


def validate(ctx: Ctx, policy: ReviewPolicy) -> None:
    """Keep schema and compatibility checks inside the selected ownership."""
    with (
        (
            nullcontext()
            if ctx.dry_run
            else store.writer_lock(ctx.data, wait_s=0)
        ),
        (
            nullcontext(lambda: None)
            if ctx.dry_run
            else snapshot_io.validated(store.db_path(ctx.data))
        ) as check,
        (
            connection(store.db_path(ctx.data))
            if ctx.dry_run
            else closing(
                sqlite3.connect(
                    store.db_path(ctx.data).as_uri() + "?mode=ro", uri=True
                )
            )
        ) as conn,
    ):
        check()
        if conn.execute("PRAGMA user_version").fetchone()[0] == 3:
            return
        conn.row_factory = sqlite3.Row
        validate_policy_source(conn)
        compatible(conn, policy)
        check()
