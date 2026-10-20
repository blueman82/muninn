"""Admit immutable review requests and fence their individual attempts."""

from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

from muninn.knowledge_models import AttemptId, RequestId, Scope
from muninn.review_claims import UNKNOWN_CONFIG as UNKNOWN_CONFIG
from muninn.review_claims import claim as _claim
from muninn.review_claims import claim_or_refuse as _claim_or_refuse
from muninn.review_claims import fence as _fence
from muninn.review_claims import request_policy as _policy
from muninn.review_evidence_models import EvidenceWindow
from muninn.review_json import canonical_json
from muninn.review_models import (
    AttemptView,
    RequestView,
    ReviewPolicy,
)
from muninn.review_progress import (
    check_window,
    checked_policy,
    digest,
    mark_eligible,
    reconciliation_sources,
    reconciliation_windows,
    require_transaction,
    source_admission,
    source_progress,
    unsuppressed_window,
)
from muninn.review_read_budget import ReadBudget
from muninn.review_request_views import attempt_view, request_row, request_view

_RETRYABLE = frozenset({"deadline", "unavailable"})
_ERRORS = _RETRYABLE | {
    "auth",
    "config",
    "budget",
    "review_policy_required",
    "rejected_validation",
    "rejected_evidence",
    "rejected_scope",
    "rejected_authority",
    "rejected_revision",
    "rejected_erased",
    "rejected_fenced",
}


def enqueue_review(
    conn: sqlite3.Connection,
    window: EvidenceWindow,
    scope: Scope,
    policy: ReviewPolicy,
    trigger: str,
    now: float,
    *,
    budget: ReadBudget | None = None,
) -> RequestView:
    """Deduplicate immutable windows, scopes and policies atomically."""
    require_transaction(conn, now)
    policy = checked_policy(policy)
    if not conn.execute("SELECT admission FROM review_control").fetchone()[0]:
        raise ValueError("review_policy_required")
    if trigger not in {"explicit", "reconcile", "hook", "ingest"}:
        raise ValueError("invalid_trigger")
    key = hashlib.sha256(
        (
            canonical_json(window)
            + canonical_json(scope)
            + canonical_json(policy)
        ).encode()
    ).hexdigest()
    existing = conn.execute(
        "SELECT * FROM review_request WHERE duplicate_key=?", (key,)
    ).fetchone()
    budget = (
        budget if budget is not None else ReadBudget(policy.max_input_bytes)
    )
    if existing is not None:
        if existing["state"] not in {"cancelled", "completed"}:
            check_window(conn, window, scope, policy, budget=budget)
        return request_view(existing)
    source = check_window(conn, window, scope, policy, budget=budget)
    source_progress(conn, source, now)
    ident = f"R_{uuid4().hex}"
    conn.execute(
        "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
        "session_root,from_line,through_line,boundary_hash,scope_json,"
        "policy_json,policy_digest,trigger,origin,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'review','pending',?)",
        (
            ident,
            key,
            window.source.provider,
            window.source.thread_id,
            source["session_root"],
            window.from_line,
            window.through_line,
            window.boundary_hash,
            canonical_json(scope),
            canonical_json(policy),
            digest(policy),
            trigger,
            now,
        ),
    )
    mark_eligible(conn, window, now)
    return request_view(request_row(conn, ident))


def reconcile_sources(
    conn: sqlite3.Connection,
    policy: ReviewPolicy,
    now: float,
) -> tuple[RequestId, ...]:
    """Admit complete new intervals without reviving cancellation."""
    require_transaction(conn, now)
    policy = checked_policy(policy)
    control = conn.execute("SELECT * FROM review_control").fetchone()
    if not control["admission"] or control["activated_at"] is None:
        return ()
    if control["policy_digest"] != digest(policy):
        raise ValueError("review_policy_required")
    sources = reconciliation_sources(conn)
    if not sources:
        return ()
    shares: tuple[ReadBudget, ...] = ()
    with source_admission(conn, now):
        shares = ReadBudget(policy.max_input_bytes).partition(len(sources))
    if not shares:
        return ()
    output: list[RequestId] = []
    for source, budget in zip(sources, shares, strict=True):
        with source_admission(conn, now) as admitted:
            for window, scope in reconciliation_windows(
                conn, policy, now, source, budget=budget
            ):
                permitted = unsuppressed_window(conn, window)
                if permitted is None:
                    mark_eligible(conn, window, now)
                    continue
                request = enqueue_review(
                    conn,
                    permitted,
                    scope,
                    policy,
                    "reconcile",
                    now,
                    budget=budget,
                )
                if request.state == "pending":
                    admitted.append(request.id)
        output.extend(admitted)
    return tuple(output)


def claim_attempt(
    conn: sqlite3.Connection,
    request_id: RequestId,
    generation: int,
    now: float,
) -> AttemptView:
    """Create one current attempt after checking readiness and policy."""
    return _claim(conn, request_id, generation, now, proposal=False)


def claim_or_refuse_attempt(
    conn: sqlite3.Connection,
    request_id: RequestId,
    generation: int,
    now: float,
) -> AttemptView | RequestView:
    """Claim ready work or durably refuse its unavailable source evidence."""
    return _claim_or_refuse(conn, request_id, generation, now)


def claim_proposal(
    conn: sqlite3.Connection,
    request_id: RequestId,
    generation: int,
    now: float,
) -> AttemptView:
    """Fence prior work and claim a checked local proposal atomically."""
    return _claim(conn, request_id, generation, now, proposal=True)


def cancel_request(
    conn: sqlite3.Connection,
    request_id: RequestId,
    now: float,
) -> RequestView:
    """Cancel a request until explicit retry, preserving its receipt."""
    require_transaction(conn, now)
    row = request_row(conn, request_id)
    if conn.execute(
        "SELECT 1 FROM review_receipt WHERE request_id=?", (request_id,)
    ).fetchone():
        return request_view(row)
    _fence(conn, row, "operator_cancelled", now)
    conn.execute(
        "UPDATE review_request SET state='cancelled',retry_at=NULL,"
        "cancel_code='operator_cancelled',current_attempt_id=NULL WHERE id=?",
        (request_id,),
    )
    return request_view(request_row(conn, request_id))


def retry_request(
    conn: sqlite3.Connection,
    request_id: RequestId,
    now: float,
) -> RequestView:
    """Reopen the same permitted request for a later fresh attempt."""
    require_transaction(conn, now)
    row = request_row(conn, request_id)
    if conn.execute(
        "SELECT 1 FROM review_receipt WHERE request_id=?", (request_id,)
    ).fetchone():
        return request_view(row)
    policy = checked_policy(
        ReviewPolicy.model_validate_json(row["policy_json"])
    )
    _policy(conn, row, budget=ReadBudget(policy.max_input_bytes))
    if row["state"] not in {"cancelled", "failed", "waiting"}:
        raise ValueError("request_not_retryable")
    _fence(conn, row, "operator_retry", now)
    conn.execute(
        "UPDATE review_request SET state='pending',retry_at=NULL,"
        "cancel_code='operator_retry',current_attempt_id=NULL WHERE id=?",
        (request_id,),
    )
    return request_view(request_row(conn, request_id))


def fail_attempt(
    conn: sqlite3.Connection,
    attempt_id: AttemptId,
    error_code: str,
    now: float,
) -> RequestView:
    """Record finite failure codes and allow only bounded transient retries."""
    require_transaction(conn, now)
    if error_code not in _ERRORS:
        raise ValueError("invalid_error_code")
    attempt = attempt_view(conn, attempt_id)
    row = request_row(conn, attempt.request_id)
    generation = conn.execute(
        "SELECT generation FROM review_control"
    ).fetchone()[0]
    if (
        row["current_attempt_id"] != attempt_id
        or row["state"] != "running"
        or attempt.generation != generation
    ):
        raise ValueError("rejected_fenced")
    _fence(conn, row, "failed", now)
    conn.execute(
        "UPDATE review_attempt SET error_code=? WHERE id=?",
        (error_code, attempt_id),
    )
    _retry_state(conn, row, error_code in _RETRYABLE, now)
    return request_view(request_row(conn, attempt.request_id))


def _retry_state(
    conn: sqlite3.Connection, row: sqlite3.Row, permitted: bool, now: float
) -> None:
    """Apply the stored finite retry schedule without changing its policy."""
    policy = ReviewPolicy.model_validate_json(row["policy_json"])
    count: int = conn.execute(
        "SELECT count(*) FROM review_attempt WHERE request_id=?", (row["id"],)
    ).fetchone()[0]
    retry_at: float | None = (
        now + policy.retry_backoff_seconds[count - 1]
        if permitted and 0 < count < policy.max_attempts
        else None
    )
    conn.execute(
        "UPDATE review_request SET "
        "state=?,retry_at=?,current_attempt_id=NULL WHERE id=?",
        ("waiting" if retry_at is not None else "failed", retry_at, row["id"]),
    )


def recover_attempts(
    conn: sqlite3.Connection,
    generation: int,
    now: float,
) -> tuple[RequestId, ...]:
    """Fence abandoned generations while preserving cancellation."""
    require_transaction(conn, now)
    control = conn.execute("SELECT generation FROM review_control").fetchone()[
        0
    ]
    if type(generation) is not int or generation < 1 or generation != control:
        raise ValueError("rejected_fenced")
    rows = conn.execute(
        "SELECT r.* FROM review_request r JOIN review_attempt a "
        "ON a.id=r.current_attempt_id WHERE a.generation<? AND "
        "a.ended_at IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM review_receipt c WHERE "
        "c.request_id=r.id)",
        (generation,),
    ).fetchall()
    recovered: list[RequestId] = []
    for row in rows:
        _fence(conn, row, "abandoned", now)
        if row["state"] != "cancelled":
            _retry_state(conn, row, True, now)
            recovered.append(row["id"])
    return tuple(recovered)
