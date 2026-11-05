"""Contain and validate an existing marked store without replacing its file."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from install import record, snapshot_io
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import file_sync, review_requests, store
from muninn import review_pause as q
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy, digest


def _fence_current(conn: sqlite3.Connection) -> sqlite3.Row:
    """Commit the stopped admission and fence all abandoned attempts."""
    control = conn.execute("SELECT * FROM review_control").fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE review_control SET admission=0,generation=generation+1"
        )
        review_requests.recover_attempts(
            conn, control["generation"] + 1, time.time()
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return control


def _restore_admission(conn: sqlite3.Connection, control: sqlite3.Row) -> None:
    """Validate current data and restore only the prior permitted policy."""
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None or (
        [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]
    ):
        raise StepFailedError("current_store_invalid")
    if not control["admission"]:
        return
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(control["policy_json"])
        )
        if control["policy_digest"] != digest(policy):
            raise ValueError("policy_digest_mismatch")
    except ValueError:
        raise ValueError("review_policy_required") from None
    conn.execute("UPDATE review_control SET admission=1")


def prepare_current(ctx: Ctx, rec: Record, owner: q.MaintenanceOwner) -> Path:
    """Maintain a verified current file without replacing its pathname."""
    path = store.db_path(ctx.data)
    with (
        q.maintenance_writer(owner),
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
            q.verify_current(owner, conn)
            control = _fence_current(conn)
            _restore_admission(conn, control)
            revalidate()
        file_sync.sync_path(path)
        revalidate()
        rec["store"]["phase"] = "current_prepared"
        record.save(ctx, rec)
    return path
