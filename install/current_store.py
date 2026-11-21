"""Contain and validate an existing marked store without replacing its file."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from install import record, snapshot_io
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import (
    cli_core,
    file_sync,
    platform_rebuild,
    review_requests,
    review_state,
    review_unknown_state,
    store,
)
from muninn.tombstones import reapply_tombstones


def _fence_current(conn: sqlite3.Connection) -> None:
    """Fence all abandoned attempts."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        review_unknown_state.ensure(conn)
        review_requests.recover_attempts(conn, time.time())
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _check_current(conn: sqlite3.Connection) -> None:
    """Validate current data."""
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None or (
        [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]
    ):
        raise StepFailedError("current_store_invalid")


def prepare_current(ctx: Ctx, rec: Record) -> Path:
    """Maintain a verified current file without replacing its pathname."""
    path = store.db_path(ctx.data)
    with (
        store.writer_lock(ctx.data, wait_s=cli_core.WRITER_WAIT_S),
        snapshot_io.validated(path) as revalidate,
    ):
        try:
            with closing(store.connect_ro(path)) as conn:
                revalidate()
                store.assert_store_format(conn)
        except store.StoreUnavailableError as exc:
            raise StepFailedError(str(exc)) from exc
        rec["store"] = {
            "phase": "current_mutation_possible",
            "path": str(path),
            "file_identity": [path.stat().st_dev, path.stat().st_ino],
        }
        record.save(ctx, rec)
        with closing(store.connect_rw(path)) as conn:
            revalidate()
            reapply_tombstones(conn, ctx.data)
            needs_metadata = not review_state.available(
                conn, "review_unknown_ack"
            )
        if needs_metadata:
            revalidate()
            archive = platform_rebuild.retain_upgrade_store(ctx.data, path)
            rec["store"]["archive"] = str(archive)
            record.save(ctx, rec)
            revalidate()
        with closing(store.connect_rw(path)) as conn:
            revalidate()
            _fence_current(conn)
            _check_current(conn)
            revalidate()
        file_sync.sync_path(path)
        revalidate()
        rec["store"]["phase"] = "current_prepared"
        record.save(ctx, rec)
    return path
