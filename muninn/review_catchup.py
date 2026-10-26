"""Recover one checked indexed interval without replaying historical hooks."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from muninn import classify, hook_learn, ingest, review_state
from muninn.cli_learn import installed_policy
from muninn.hook_context import as_payload
from muninn.knowledge_models import Provider
from muninn.review_models import ReviewPolicy
from muninn.review_originals import originals
from muninn.review_progress import (
    mark_eligible,
    reconciliation_windows,
    require_transaction,
    source_admission,
    source_progress,
    unsuppressed_window,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_requests import enqueue_review
from muninn.review_selectors import decode


def _native(
    conn: sqlite3.Connection, source: sqlite3.Row, budget: ReadBudget
) -> bool:
    """Reuse native hook identity checks without replaying an event."""
    root = ingest.default_roots().get(source["root"])
    if source["provider"] == "cursor" or root is None:
        raise ValueError("source_unavailable")
    verified = hook_learn.validated_source(
        conn,
        cast(Provider, source["provider"]),
        {
            "session_id": source["session_root"],
            "transcript_path": str((root / source["path"]).resolve()),
        },
        budget,
    )
    if verified["id"] != source["id"]:
        raise ValueError("source_unavailable")
    known = conn.execute(
        "SELECT 1 FROM review_source WHERE provider=? AND thread_id=?",
        (source["provider"], source["thread_id"]),
    ).fetchone()
    activated = conn.execute(
        "SELECT activated_at FROM review_control"
    ).fetchone()[0]
    saved = review_state.selection(conn)
    if (
        known
        or source["first_seen"] <= activated
        or (saved is not None and saved.mode == "all")
    ):
        return True
    raw = originals(conn, source, 1, 1, budget=budget)[0][1]
    if classify.record_hash(raw) != source["first_line_sha256"]:
        raise ValueError("evidence_changed")
    try:
        first = decode(raw)
    except ValueError:
        raise ValueError("source_unavailable") from None
    metadata = (
        as_payload(first.get("payload"))
        if source["provider"] == "codex"
        else first
    )
    value = metadata.get("timestamp")
    if not isinstance(value, str):
        return False
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return False
    return created.tzinfo is not None and created.timestamp() >= activated


def _admit(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    policy: ReviewPolicy,
    now: float,
    budget: ReadBudget,
) -> None:
    """Keep permanent denials examined without inventing successful work."""
    candidate = 0
    denied = False
    with source_admission(conn, now):
        try:
            eligible = _native(conn, source, budget)
        except ValueError as exc:
            if str(exc) != "source_unavailable":
                raise
            return
        if not eligible:
            return
        source_progress(conn, source, now)
        conn.execute("SAVEPOINT review_catchup_window")
        try:
            for window, scope in reconciliation_windows(
                conn, policy, now, source, budget=budget, limit=1
            ):
                candidate = window.through_line
                permitted = unsuppressed_window(conn, window)
                if permitted is None:
                    mark_eligible(conn, window, now)
                else:
                    enqueue_review(
                        conn,
                        permitted,
                        scope,
                        policy,
                        "reconcile",
                        now,
                        budget=budget,
                    )
        except ValueError as exc:
            conn.execute("ROLLBACK TO review_catchup_window")
            if str(exc) not in {"rejected_scope", "evidence_erased"}:
                raise
            denied = True
        finally:
            conn.execute("RELEASE review_catchup_window")
        if denied and candidate:
            conn.execute(
                "UPDATE review_source SET examined_line=?,updated_at=? "
                "WHERE provider=? AND thread_id=?",
                (candidate, now, source["provider"], source["thread_id"]),
            )


def catch_up(
    conn: sqlite3.Connection,
    policy: ReviewPolicy,
    now: float,
    after_id: int,
) -> int:
    """Give one indexed source a turn under the exact installed policy."""
    require_transaction(conn, now)
    try:
        current = installed_policy(conn)
    except ValueError:
        return after_id
    if current != policy:
        return after_id
    if (
        conn.execute("SELECT activated_at FROM review_control").fetchone()[0]
        is None
    ):
        return after_id
    after_id, source = _pending_source(conn, after_id)
    if source is None:
        return after_id
    budget = ReadBudget(policy.max_input_bytes)
    _admit(conn, source, policy, now, budget)
    return source["id"]


def _pending_source(
    conn: sqlite3.Connection, after_id: int
) -> tuple[int, sqlite3.Row | None]:
    """Skip a bounded batch of idle sources before reading one original."""
    ids = conn.execute(
        "SELECT id FROM source WHERE id>? ORDER BY id LIMIT 256",
        (after_id,),
    ).fetchall()
    if not ids and after_id:
        after_id = 0
        ids = conn.execute(
            "SELECT id FROM source ORDER BY id LIMIT 256"
        ).fetchall()
    if not ids:
        return 0, None
    last = ids[-1][0]
    source = conn.execute(
        "SELECT s.* FROM source s LEFT JOIN review_source r "
        "ON r.provider=s.provider AND r.thread_id=s.thread_id "
        "WHERE s.id>? AND s.id<=? AND s.provider IN ('codex','claude') "
        "AND s.status='active' AND s.thread_class='primary' "
        "AND s.replay_mode='none' AND s.parent_thread_id IS NULL "
        "AND s.forked_from_id IS NULL AND s.cursor_line>"
        "max(coalesce(r.eligible_line,0),coalesce(r.examined_line,0)) "
        "ORDER BY s.id LIMIT 1",
        (after_id, last),
    ).fetchone()
    return last, source
