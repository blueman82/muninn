"""Check exact uncertainty acknowledgements without widening call states."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable

from muninn import review_unknown_state
from muninn.knowledge_models import AttemptId
from muninn.review_budget import call_view
from muninn.review_json import canonical_json_value
from muninn.review_runtime_models import ManagedCallRecord
from muninn.review_unknown_state import UnknownAcknowledgement


def row_digest(row: sqlite3.Row) -> str:
    """Hash all persisted scalar values without storing their contents."""
    return hashlib.sha256(canonical_json_value(dict(row)).encode()).hexdigest()


def snapshots(
    conn: sqlite3.Connection, attempt: AttemptId, round_index: int
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """Require the exact unknown call and its ended failed lineage."""
    call = conn.execute(
        "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
        (attempt, round_index),
    ).fetchone()
    parent = conn.execute(
        "SELECT * FROM review_attempt WHERE id=?", (attempt,)
    ).fetchone()
    request = (
        None
        if parent is None
        else conn.execute(
            "SELECT * FROM review_request WHERE id=?", (parent["request_id"],)
        ).fetchone()
    )
    if (
        call is None
        or parent is None
        or request is None
        or call["state"] != "unknown"
        or call["launched_at"] is not None
        or call["process_identity_json"] is not None
        or parent["ended_at"] is None
        or parent["outcome"] != "failed"
        or request["state"] != "failed"
        or request["retry_at"] is not None
        or request["current_attempt_id"] is not None
    ):
        raise ValueError("recovery_refused")
    recorded = call_view(call).requested
    if (
        isinstance(recorded, ManagedCallRecord)
        and recorded.launched_identity is not None
    ):
        raise ValueError("recovery_refused")
    return call, parent, request


def matches(
    conn: sqlite3.Connection, acknowledgement: UnknownAcknowledgement
) -> bool:
    """Require unchanged history behind the recorded generation fence."""
    try:
        call, parent, request = snapshots(
            conn, acknowledgement.attempt_id, acknowledgement.round_index
        )
        control = conn.execute("SELECT * FROM review_control").fetchone()
        return (
            control is not None
            and control["generation"] >= acknowledgement.resulting_generation
            and parent["generation"] < acknowledgement.resulting_generation
            and call["policy_digest"] == acknowledgement.call_policy_digest
            and request["policy_digest"]
            == acknowledgement.request_policy_digest
            and request["id"] == acknowledgement.request_id
            and row_digest(call) == acknowledgement.call_digest
            and row_digest(parent) == acknowledgement.attempt_digest
            and row_digest(request) == acknowledgement.request_digest
        )
    except ValueError:
        return False


def acknowledged(conn: sqlite3.Connection, call: sqlite3.Row) -> bool:
    """Exempt only an exact, still valid explicit acceptance record."""
    try:
        acknowledgement = review_unknown_state.read(
            conn, call["attempt_id"], call["round_index"]
        )
        return acknowledgement is not None and matches(conn, acknowledgement)
    except ValueError:
        return False


def unsafe_calls(
    conn: sqlite3.Connection,
    states: Iterable[str] = ("reserved", "launched", "unknown"),
    *,
    selected: tuple[AttemptId, int] | None = None,
) -> bool:
    """Find unaccepted calls without changing or retiring historical rows."""
    wanted = frozenset(states)
    return any(
        call["state"] in wanted
        and (call["attempt_id"], call["round_index"]) != selected
        and not acknowledged(conn, call)
        for call in conn.execute(
            "SELECT * FROM review_call WHERE state!='completed'"
        )
    )
