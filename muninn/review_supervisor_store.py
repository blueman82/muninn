"""Own short main-thread transactions for supervisor lifecycle state."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager
from pathlib import Path

from muninn import review_recovery, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_process import (
    ProcessEvent,
    ProcessOutcome,
    WorkerLaunched,
    WorkerReady,
)
from muninn.review_progress import digest


def is_contention(error: sqlite3.OperationalError) -> bool:
    """Recognize only SQLite's primary busy and locked result codes."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


@contextmanager
def database(
    home: Path, *, transaction: bool = True
) -> Generator[sqlite3.Connection]:
    """Close SQLite before releasing the non-reentrant writer descriptor."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("main_thread_required")
    with (
        store.writer_lock(home, wait_s=0),
        closing(store.connect_rw(store.db_path(home))) as conn,
    ):
        if transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if transaction:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise


def startup_snapshot(
    conn: sqlite3.Connection, policy: ReviewPolicy
) -> tuple[int, tuple[tuple[str, int, str, str], ...]]:
    """Check policy and capture eligible persisted identities."""
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if (
        not control["admission"]
        or control["policy_digest"] != digest(policy)
        or control["policy_json"] is None
        or ReviewPolicy.model_validate_json(control["policy_json"]) != policy
    ):
        raise ValueError("review_policy_required")
    identities = tuple(
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT attempt_id,round_index,state,process_identity_json "
            "FROM review_call WHERE state IN ('reserved','launched') "
            "AND process_identity_json IS NOT NULL "
            "ORDER BY attempt_id,round_index"
        )
    )
    return control["generation"], identities


def halt_interrupted(conn: sqlite3.Connection, policy: ReviewPolicy) -> None:
    """Keep uncertain ongoing calls stopped across shutdown and restart."""
    if (
        policy.experimental_codex is not None
        and conn.execute(
            (
                "SELECT 1 FROM review_attempt "
                "WHERE ended_at IS NULL UNION ALL "
                if not review_recovery.enabled(policy)
                else ""
            )
            + "SELECT 1 FROM review_call WHERE state IN "
            "('reserved','launched','unknown') LIMIT 1"
        ).fetchone()
    ):
        review_recovery.record(
            conn, "setup", "process_identity_unverified", "recovery", 0.0
        )
        conn.execute("UPDATE review_control SET admission=0")


def persist_event(home: Path, event: ProcessEvent, now: float) -> None:
    """Persist lifecycle data without granting stale attempt authority."""
    with database(home) as conn:
        policy_row = conn.execute(
            "SELECT policy_json FROM review_control"
        ).fetchone()
        policy = ReviewPolicy.model_validate_json(policy_row[0])
        if isinstance(event, WorkerReady):
            conn.execute(
                "UPDATE review_call SET process_identity_json=? "
                "WHERE attempt_id=? AND round_index=? "
                "AND state='reserved'",
                (
                    event.identity.model_dump_json(),
                    event.attempt_id,
                    event.round_index,
                ),
            )
        elif isinstance(event, WorkerLaunched):
            conn.execute(
                "UPDATE review_call SET state='launched',launched_at=? "
                "WHERE attempt_id=? AND round_index=? "
                "AND state='reserved'",
                (event.launched_at, event.attempt_id, event.round_index),
            )
        else:
            conn.execute(
                "UPDATE review_call SET state=?,ended_at=?,observed_json=?,"
                "usage_json=?,outcome=? WHERE attempt_id=? AND round_index=? "
                "AND state IN ('reserved','launched','completed')",
                (
                    (
                        "completed"
                        if event.error_code is None
                        or review_recovery.enabled(policy)
                        else "unknown"
                    ),
                    now,
                    canonical_json(event.observed),
                    canonical_json(event.usage) if event.usage else None,
                    event.error_code or "completed",
                    event.attempt_id,
                    event.round_index,
                ),
            )
        if (
            policy.experimental_codex is not None
            and isinstance(event, ProcessOutcome)
            and event.error_code is not None
        ) and (
            not review_recovery.enabled(policy)
            or event.error_code == "native_tool_event"
        ):
            review_recovery.record(
                conn, "setup", event.error_code or "config", "runner", now
            )
            conn.execute("UPDATE review_control SET admission=0")


def retire_unlaunched(
    conn: sqlite3.Connection, keys: Iterable[tuple[str, int]], now: float
) -> None:
    """Close reservations proven unlaunched by the current owner."""
    conn.executemany(
        "UPDATE review_call SET state='completed',ended_at=?,"
        "outcome='not_launched' WHERE attempt_id=? AND round_index=? "
        "AND state='reserved' AND process_identity_json IS NULL "
        "AND launched_at IS NULL",
        ((now, attempt, index) for attempt, index in keys),
    )
