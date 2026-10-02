"""Steps that move a pre-rename install onto the muninn names.

``legacy_copy`` stops the old poller and copies its data under the old
writer lock; the normal pin, start, config and verify steps then install the
new names; ``legacy_check`` proves the new store holds every old row; and
``legacy_remove`` deletes the old install only once everything has passed.
Nothing old is touched until then, so a failure leaves it runnable.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
from pathlib import Path

from install import configedit as ce
from install.context import Ctx, StepFailedError, dry, job, wait
from install.record import Record

OLD_DB_PREFIX, NEW_DB_PREFIX = "pctx.", "muninn."
# Left by a poller that died mid-write; copying around one would lose rows.
_HOT = ("pctx.sqlite-journal", "pctx.sqlite-wal")
OLD_INSTALL_DIR_GLOB = "provenance-context-install-*"
# Row counts of the old store at copy time, kept until the old install is
# gone. Written only after the new install verified, so its presence means
# "the new install is good and the old leftovers may be removed".
COUNTS_FILE = "legacy-counts.json"


def table_counts(db: Path) -> dict[str, int]:
    """Count the rows of every table in a store, opening it read-only.

    Args:
        db: The SQLite file.

    Returns:
        Row count per table name.

    Raises:
        StepFailedError: If the file does not open or fails its check.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StepFailedError(f"{db.name} failed its integrity check")
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return {
                n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]
                for n in names
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StepFailedError(f"cannot read {db}: {exc}") from exc


def _new_blockers(ctx: Ctx) -> list[Path]:
    """List the muninn paths that exist, ignoring an empty data dir.

    A stray run can leave an empty data dir; it is removed so it never
    blocks the move (only ignored in a dry run).

    Returns:
        The paths that block the move.
    """
    empty = ctx.data.is_dir() and not any(ctx.data.iterdir())
    if empty and not ctx.dry_run:
        ctx.data.rmdir()
    new = (ctx.data, ctx.plist, ctx.lib / "current")
    return [
        p
        for p in new
        if (p.exists() or p.is_symlink()) and not (empty and p == ctx.data)
    ]


def resume_pending(ctx: Ctx) -> bool:
    """Say whether a finished move still has old leftovers to remove."""
    return (ctx.lib / COUNTS_FILE).is_file() and ctx.has_old_install()


def check_old_store(ctx: Ctx) -> None:
    """Refuse a migration the old install cannot support.

    Args:
        ctx: The run context.

    Raises:
        StepFailedError: If the old install is incomplete, a new install
            also exists, or the old store has a hot journal.
    """
    blockers = _new_blockers(ctx)
    if blockers:
        raise StepFailedError(
            f"a muninn install already exists beside the pre-rename one "
            f"({blockers[0]}). Your old data in {ctx.old_data} is "
            f"untouched. Move or delete {blockers[0]} by hand, then run "
            "this again"
        )
    parts = (ctx.old_data / "pctx.sqlite", ctx.old_plist)
    if (
        not all(p.exists() for p in parts)
        or not (ctx.old_lib / "current").is_symlink()
    ):
        raise StepFailedError("pre-rename install is incomplete: fix by hand")
    check_old_store_files(ctx)


def _copy(ctx: Ctx, rec: Record) -> None:
    """Copy the old data dir to the new name and prove the counts match."""
    tmp = ctx.data.with_name(f".muninn.tmp-{ctx.ts}")
    shutil.copytree(ctx.old_data, tmp, symlinks=True)
    for path in sorted(tmp.iterdir()):
        if path.name.startswith(OLD_DB_PREFIX):
            new = NEW_DB_PREFIX + path.name.removeprefix(OLD_DB_PREFIX)
            path.rename(tmp / new)
    want = table_counts(ctx.old_data / "pctx.sqlite")
    if table_counts(tmp / "muninn.sqlite") != want:
        raise StepFailedError("copied store does not match the old one")
    rec["legacy_counts"] = want
    tmp.rename(ctx.data)


def legacy_copy(ctx: Ctx, rec: Record) -> None:
    """Stop the old poller, lock the old store and copy it to muninn.

    Raises:
        StepFailedError: If the old job will not stop or its writer lock is
            held (busy), so rollback restarts the old job.
    """
    if dry(
        ctx,
        f"would stop the old poller ({ctx.old_target}), copy {ctx.old_data} "
        f"to {ctx.data} (pctx.sqlite becomes muninn.sqlite) and check the "
        "row counts match",
    ):
        return
    ctx.run(["launchctl", "bootout", ctx.old_target])
    wait(
        ctx,
        lambda: job(ctx, ctx.old_target) is None,
        30,
        "old job still loaded",
    )
    fd = os.open(ctx.old_data / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StepFailedError(
                "busy: the old store's writer lock is held; try again"
            ) from None
        check_old_store_files(ctx)
        _copy(ctx, rec)
    finally:
        os.close(fd)


def check_old_store_files(ctx: Ctx) -> None:
    """Re-check for a hot journal now that the old poller is stopped."""
    if any((ctx.old_data / n).exists() for n in _HOT):
        raise StepFailedError("pre-rename store has a hot journal: fix first")


def legacy_check(ctx: Ctx, rec: Record) -> None:
    """Prove the new store opens and holds every row the old one had.

    Counts may have grown since the copy (the new poller ingests), never
    shrunk. On success the copy-time counts are kept for ``legacy_remove``.

    Raises:
        StepFailedError: If a table is missing or has fewer rows.
    """
    if dry(
        ctx,
        "would check the new store holds every old row, then delete the old "
        f"install: {ctx.old_lib}, {ctx.old_data}, {ctx.old_plist}, "
        f"{ctx.old_cli} and the old Codex cache (no way back)",
    ):
        return
    have = table_counts(ctx.data / "muninn.sqlite")
    for table, count in rec["legacy_counts"].items():
        if have.get(table, -1) < count:
            raise StepFailedError(f"new store lost rows in {table}")
    ce.atomic_write(
        ctx.lib / COUNTS_FILE, json.dumps(rec["legacy_counts"]).encode(), 0o600
    )


def _rm(path: Path) -> None:
    """Delete a file, link or directory; already gone is fine."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StepFailedError(
            f"could not delete {path}: {exc}. The new install is good; run "
            "the same upgrade again to finish removing the old one"
        ) from exc


def _check_unchanged(ctx: Ctx) -> None:
    """Refuse to delete an old store that took rows after it was copied."""
    want = json.loads((ctx.lib / COUNTS_FILE).read_bytes())
    check_old_store_files(ctx)
    if table_counts(ctx.old_data / "pctx.sqlite") != want:
        raise StepFailedError(
            "the old store changed after it was copied (something still "
            "wrote to it), so nothing was deleted. Your old data in "
            f"{ctx.old_data} is intact; the new install is in place. Copy "
            "what you need by hand, then delete the old files yourself"
        )


def legacy_remove(ctx: Ctx) -> None:
    """Delete the old install; safe to run again after a partial failure.

    The old poller is stopped and the old writer lock held while the old
    store is compared with its copy-time counts, so nothing is deleted if a
    row arrived after the copy. The old data dir goes last.

    Raises:
        StepFailedError: If the old store is busy or changed, or a deletion
            fails.
    """
    ctx.run(["launchctl", "bootout", ctx.old_target])  # may be gone already
    wait(
        ctx,
        lambda: job(ctx, ctx.old_target) is None,
        30,
        "old job still loaded",
    )
    fd = -1
    if ctx.old_data.is_dir():
        fd = os.open(
            ctx.old_data / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
    try:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StepFailedError(
                    "busy: the old store's writer lock is held; nothing "
                    "was deleted, try again"
                ) from None
            if (ctx.old_data / "pctx.sqlite").exists():
                _check_unchanged(ctx)
        stale = list(ctx.data.parent.glob(OLD_INSTALL_DIR_GLOB))
        for path in (
            ctx.old_plist,
            ctx.old_cli,
            ctx.old_lib,
            ctx.old_cache,
            *stale,
            ctx.old_data,
        ):
            _rm(path)
    finally:
        if fd >= 0:
            os.close(fd)
    (ctx.lib / COUNTS_FILE).unlink(missing_ok=True)
    ctx.say("removed the pre-rename install; muninn now owns the data")
