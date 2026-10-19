"""Share checked request admission and attempt fencing across claim paths."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from muninn.knowledge_models import AttemptId, RequestId
from muninn.review_evidence import validate_evidence
from muninn.review_json import canonical_json
from muninn.review_models import (
    AttemptView,
    ObservedConfig,
    RequestView,
    ReviewPolicy,
)
from muninn.review_progress import (
    check_window,
    checked_policy,
    digest,
    require_transaction,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_request_views import attempt_view, request_row, request_view
from muninn.tombstones import require_current_tombstones

UNKNOWN_CONFIG = ObservedConfig(
    runner=None, version=None, model=None, reasoning=None
)


_SOURCE_REFUSALS = frozenset(
    {
        "source_unavailable",
        "original_unavailable",
        "ancestry_unavailable",
        "evidence_limit",
        "incomplete_window",
        "evidence_changed",
        "evidence_unavailable",
        "evidence_erased",
        "rejected_scope",
    }
)


def _admitted_policy(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> ReviewPolicy:
    """Require the immutable snapshot to match active admission policy."""
    policy = checked_policy(
        ReviewPolicy.model_validate_json(row["policy_json"])
    )
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if (
        not control["admission"]
        or control["policy_digest"] != digest(policy)
        or row["policy_digest"] != digest(policy)
        or control["policy_json"] is None
        or canonical_json(
            ReviewPolicy.model_validate_json(control["policy_json"])
        )
        != row["policy_json"]
    ):
        raise ValueError("review_policy_required")
    return policy


def _sources(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    policy: ReviewPolicy,
    *,
    budget: ReadBudget,
) -> None:
    """Read current window and evidence under the same original-read grant."""
    view = request_view(row)
    check_window(conn, view.window, view.scope, policy, budget=budget)
    evidence = tuple(
        r[0]
        for r in conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (row["id"],),
        )
    )
    if evidence:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        validate_evidence(
            conn,
            Path(path).parent,
            evidence,
            view.scope,
            policy,
            budget=budget,
        )


def request_policy(
    conn: sqlite3.Connection, row: sqlite3.Row, *, budget: ReadBudget
) -> ReviewPolicy:
    """Check admission and source evidence for an existing request."""
    policy = _admitted_policy(conn, row)
    _sources(conn, row, policy, budget=budget)
    return policy


def _ready(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    policy: ReviewPolicy,
    now: float,
    proposal: bool,
) -> bool:
    """Keep operator retries distinct from automatic readiness limits."""
    states = {"pending", "waiting"}
    if proposal:
        states |= {"running", "cancelled", "failed"}
    if row["state"] not in states or (
        not proposal and row["retry_at"] is not None and row["retry_at"] > now
    ):
        raise ValueError("request_not_ready")
    active = conn.execute(
        "SELECT count(*) FROM review_request WHERE state='running' AND id!=?",
        (row["id"],),
    ).fetchone()[0]
    if active >= policy.concurrency:
        raise ValueError("review_concurrency")
    count: int = conn.execute(
        "SELECT count(*) FROM review_attempt WHERE request_id=?", (row["id"],)
    ).fetchone()[0]
    operator = proposal or row["cancel_code"] == "operator_retry"
    if count >= policy.max_attempts and not operator:
        raise ValueError("review_attempt_limit")
    return operator


def claim(
    conn: sqlite3.Connection,
    request_id: RequestId,
    generation: int,
    now: float,
    *,
    proposal: bool,
) -> AttemptView:
    """Claim one checked attempt without acquiring caller-owned locks."""
    require_transaction(conn, now)
    row = request_row(conn, request_id)
    receipt = _receipt_attempt(conn, request_id)
    if receipt is not None:
        return attempt_view(conn, receipt)
    if proposal:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        require_current_tombstones(conn, Path(path).parent)
    policy = checked_policy(
        ReviewPolicy.model_validate_json(row["policy_json"])
    )
    policy = request_policy(
        conn, row, budget=ReadBudget(policy.max_input_bytes)
    )
    _generation(conn, generation)
    operator = _ready(conn, row, policy, now, proposal)
    return _start(conn, row, policy, generation, now, proposal, operator)


def _start(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    policy: ReviewPolicy,
    generation: int,
    now: float,
    proposal: bool,
    operator: bool,
) -> AttemptView:
    """Create a checked current attempt after all admission reads succeed."""
    if proposal:
        fence(conn, row, "operator_retry", now)
    ident = f"A_{uuid4().hex}"
    conn.execute(
        "INSERT INTO review_attempt(id,request_id,generation,requested_json,"
        "observed_json,started_at,progress_at,outcome) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            ident,
            row["id"],
            generation,
            None if proposal else canonical_json(policy.runner),
            canonical_json(UNKNOWN_CONFIG),
            now,
            now,
            "operator_retry" if operator else None,
        ),
    )
    conn.execute(
        "UPDATE review_request SET state='running',current_attempt_id=?,"
        "retry_at=NULL,cancel_code=NULL,origin=? WHERE id=?",
        (ident, "proposal" if proposal else row["origin"], row["id"]),
    )
    return attempt_view(conn, ident)


def fence(
    conn: sqlite3.Connection, row: sqlite3.Row, code: str, now: float
) -> None:
    """Fence the attempt while retaining uncertain call charges."""
    _current_attempt(conn, row)
    conn.execute(
        "UPDATE review_attempt SET ended_at=?,progress_at=?,outcome=? "
        "WHERE id=? AND ended_at IS NULL",
        (now, now, code, row["current_attempt_id"]),
    )
    conn.execute(
        "UPDATE review_call SET state='unknown',ended_at=?,outcome=? "
        "WHERE attempt_id=? AND state IN ('reserved','launched')",
        (now, code, row["current_attempt_id"]),
    )


def _receipt_attempt(
    conn: sqlite3.Connection, request_id: RequestId
) -> AttemptId | None:
    """Find the authoritative success identity before mutable checks."""
    row = conn.execute(
        "SELECT attempt_id FROM review_receipt WHERE request_id=?",
        (request_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _generation(conn: sqlite3.Connection, generation: int) -> None:
    """Reject stale or invalid supervisor generation without changing it."""
    current_generation = conn.execute(
        "SELECT generation FROM review_control"
    ).fetchone()[0]
    if (
        type(generation) is not int
        or generation < 1
        or generation != current_generation
    ):
        raise ValueError("rejected_fenced")


def _current_attempt(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> sqlite3.Row | None:
    """Require any current pointer to identify this request's own attempt."""
    if row["current_attempt_id"] is None:
        return None
    attempt = conn.execute(
        "SELECT * FROM review_attempt WHERE id=?", (row["current_attempt_id"],)
    ).fetchone()
    if attempt is None or attempt["request_id"] != row["id"]:
        raise ValueError("rejected_fenced")
    return attempt


def claim_or_refuse(
    conn: sqlite3.Connection,
    request_id: RequestId,
    generation: int,
    now: float,
) -> AttemptView | RequestView:
    """Durably refuse only source admission faults before attempt creation."""
    require_transaction(conn, now)
    row = request_row(conn, request_id)
    if _receipt_attempt(conn, request_id) is not None:
        return request_view(row)
    _generation(conn, generation)
    policy = _admitted_policy(conn, row)
    operator = _ready(conn, row, policy, now, False)
    current = _current_attempt(conn, row)
    if current is not None and current["ended_at"] is None:
        raise ValueError("rejected_fenced")
    budget = ReadBudget(policy.max_input_bytes)
    try:
        _sources(conn, row, policy, budget=budget)
    except ValueError as exc:
        if type(exc) is not ValueError or str(exc) not in _SOURCE_REFUSALS:
            raise
        conn.execute(
            "UPDATE review_request SET state='failed',retry_at=NULL,"
            "current_attempt_id=NULL WHERE id=?",
            (request_id,),
        )
        return request_view(request_row(conn, request_id))
    return _start(conn, row, policy, generation, now, False, operator)
