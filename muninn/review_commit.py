"""Apply a review result under one checked, idempotent SQLite transaction."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from muninn.knowledge_models import (
    AffectedRevision,
    AttemptId,
    Digest,
    EvidenceId,
    RequestId,
    ReviewResult,
    Scope,
    StrictModel,
    UtcSeconds,
)
from muninn.knowledge_write import (
    apply_change,
    body_of,
    claims,
    json_value,
    save_unresolved,
    target,
)
from muninn.redaction import redact
from muninn.review_evidence import validate_evidence
from muninn.review_evidence_models import EvidenceRecord, PositiveInt
from muninn.review_json import canonical_json, parse_result
from muninn.review_models import Receipt, ReviewPolicy
from muninn.review_progress import (
    advance_completed,
    check_window,
    checked_policy,
    digest,
    require_transaction,
)
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_recovery import clear as clear_issue
from muninn.review_requests import fail_attempt, request_view
from muninn.review_snapshot import snapshot
from muninn.tombstones import require_current_tombstones


class CommitContext(StrictModel):
    """Carry only program-owned identities into the writer boundary."""

    home: Path
    request_id: RequestId
    attempt_id: AttemptId
    generation: PositiveInt
    evidence_ids: tuple[EvidenceId, ...]
    now: UtcSeconds
    knowledge_digest: Digest | None = None


class CommitRejectedError(ValueError):
    """Expose a finite safe rejection code without governed validation text."""


_EVIDENCE_ERRORS = {
    "evidence_erased": "rejected_erased",
    "evidence_changed": "rejected_evidence",
    "evidence_unavailable": "rejected_evidence",
    "evidence_limit": "evidence_limit",
    "evidence_policy": "rejected_validation",
    "source_unavailable": "rejected_evidence",
    "incomplete_window": "rejected_evidence",
    "rejected_scope": "rejected_scope",
    "original_unavailable": "rejected_evidence",
    "ancestry_unavailable": "rejected_evidence",
    "scope_unavailable": "rejected_scope",
    "forbidden_selector": "rejected_evidence",
    "invalid_span": "rejected_evidence",
    "redacted_span": "rejected_evidence",
    "empty_evidence": "rejected_evidence",
}


def _receipt(
    conn: sqlite3.Connection, request_id: RequestId
) -> Receipt | None:
    """Reload the one durable success before consulting mutable state."""
    row = conn.execute(
        "SELECT * FROM review_receipt WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return Receipt.model_validate_json(
        json_value(
            {
                "schema_version": 1,
                "request_id": row["request_id"],
                "attempt_id": row["attempt_id"],
                "applied": json.loads(row["applied_json"]),
                "unresolved_ids": json.loads(row["unresolved_json"]),
                "committed_at": row["committed_at"],
                "outcome": row["outcome"],
                "no_change_reason": row["no_change_reason"],
            }
        )
    )


def _current(conn: sqlite3.Connection, context: CommitContext) -> sqlite3.Row:
    """Fence obsolete, ended, cancelled, and cross-request attempts."""
    row = conn.execute(
        "SELECT r.* FROM review_request r JOIN review_attempt a "
        "ON a.id=r.current_attempt_id JOIN review_control c ON c.singleton=1 "
        "WHERE r.id=? AND r.state='running' AND a.id=? AND a.request_id=r.id "
        "AND a.ended_at IS NULL AND a.generation=? "
        "AND c.generation=a.generation",
        (context.request_id, context.attempt_id, context.generation),
    ).fetchone()
    if row is None:
        raise CommitRejectedError("rejected_fenced")
    return row


def _policy(conn: sqlite3.Connection, row: sqlite3.Row) -> ReviewPolicy:
    """Check immutable admission identities without calling admission reads."""
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(row["policy_json"])
        )
        control = conn.execute("SELECT * FROM review_control").fetchone()
        if (
            not control["admission"]
            or row["policy_digest"] != digest(policy)
            or control["policy_digest"] != digest(policy)
            or control["policy_json"] is None
            or canonical_json(
                ReviewPolicy.model_validate_json(control["policy_json"])
            )
            != canonical_json(policy)
        ):
            raise CommitRejectedError("rejected_validation")
    except ValueError:
        raise CommitRejectedError("rejected_validation") from None
    return policy


def _evidence(
    conn: sqlite3.Connection,
    context: CommitContext,
    row: sqlite3.Row,
    policy: ReviewPolicy,
) -> tuple[EvidenceRecord, ...]:
    """Recheck every durable input through its original physical locator."""
    # Controlled reads persist admission before returning any content.
    # Knowledge supports may belong to earlier windows or other sources.
    ids = tuple(
        r[0]
        for r in conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (context.request_id,),
        )
    )
    if set(ids) != set(context.evidence_ids) or len(ids) != len(
        context.evidence_ids
    ):
        raise CommitRejectedError("rejected_evidence")
    view = request_view(row)
    try:
        budget = ReadBudget(policy.max_input_bytes)
        source, _ = check_window(
            conn, view.window, view.scope, policy, budget=budget
        )
        if source["session_root"] != row["session_root"]:
            raise CommitRejectedError("rejected_evidence")
        records = validate_evidence(
            conn, context.home, ids, view.scope, policy, budget=budget
        )
    except ValidationError:
        raise CommitRejectedError("rejected_evidence") from None
    except EvidenceLimitError as exc:
        raise CommitRejectedError(exc.code) from None
    except ValueError as exc:
        code = _EVIDENCE_ERRORS.get(str(exc))
        if code is None:
            raise
        raise CommitRejectedError(code) from None
    return records


def _target(
    conn: sqlite3.Connection, ref: AffectedRevision, scope: Scope
) -> sqlite3.Row:
    """Convert expected target refusals without swallowing systemic errors."""
    try:
        return target(conn, ref, scope)
    except ValueError as exc:
        if str(exc) not in {"rejected_revision", "rejected_scope"}:
            raise
        raise CommitRejectedError(str(exc)) from None


def _support(
    refs: list[EvidenceId],
    records: dict[EvidenceId, EvidenceRecord],
    owner: bool,
) -> None:
    """Require admitted support and attributable authority where necessary."""
    if not set(refs) <= records.keys():
        raise CommitRejectedError("rejected_evidence")
    if owner and not any(
        records[r].authority == "direct_owner"
        and records[r].evidence_type == "owner_statement"
        for r in refs
    ):
        raise CommitRejectedError("rejected_authority")


def _batch(
    conn: sqlite3.Connection,
    result: ReviewResult,
    scope: Scope,
    evidence: tuple[EvidenceRecord, ...],
) -> None:
    """Validate the whole batch before writing any governed content."""
    records = {r.id: r for r in evidence}
    replaced: set[str] = set()
    for change in result.changes:
        if change.scope != scope:
            raise CommitRejectedError("rejected_scope")
        owner = (
            change.kind in {"preference", "decision"}
            or scope.project_id is None
        )
        if change.action == "replace":
            if change.knowledge_id in replaced:
                raise CommitRejectedError("rejected_revision")
            replaced.add(change.knowledge_id)
            old = _target(
                conn,
                AffectedRevision(
                    knowledge_id=change.knowledge_id,
                    revision=change.expected_revision,
                ),
                scope,
            )
            owner = owner or old["kind"] in {"preference", "decision"}
        for _, _, refs, _ in claims(body_of(change)):
            _support(refs, records, owner)
    for proposal in result.unresolved:
        _support(proposal.evidence_refs, records, False)
        seen: set[str] = set()
        for affected in proposal.affected:
            if affected.knowledge_id in replaced | seen:
                raise CommitRejectedError("rejected_revision")
            seen.add(affected.knowledge_id)
            _target(conn, affected, scope)


def _complete(
    conn: sqlite3.Connection,
    context: CommitContext,
    row: sqlite3.Row,
    result: ReviewResult,
) -> Receipt:
    """Write content, receipt, lifecycle completion and contiguous coverage."""
    applied = [
        apply_change(conn, change, row, context.now)
        for change in result.changes
    ]
    unresolved = save_unresolved(
        conn, context.request_id, result.unresolved, context.now
    )
    receipt = Receipt(
        schema_version=1,
        request_id=context.request_id,
        attempt_id=context.attempt_id,
        applied=applied,
        unresolved_ids=unresolved,
        committed_at=context.now,
        outcome=(
            "completed_unresolved"
            if unresolved
            else "completed" if applied else "no_change"
        ),
        no_change_reason=(
            redact(result.no_change_reason)[0]
            if result.no_change_reason is not None
            else None
        ),
    )
    conn.execute(
        "INSERT INTO review_receipt VALUES(?,?,?,?,?,?,?)",
        (
            receipt.request_id,
            receipt.attempt_id,
            json_value([item.model_dump() for item in applied]),
            json_value(unresolved),
            context.now,
            receipt.outcome,
            receipt.no_change_reason,
        ),
    )
    conn.execute(
        "UPDATE review_request SET state='completed',retry_at=NULL WHERE id=?",
        (context.request_id,),
    )
    conn.execute(
        "UPDATE review_attempt SET ended_at=?,progress_at=?,outcome=?,"
        "error_code=NULL "
        "WHERE id=?",
        (context.now, context.now, receipt.outcome, context.attempt_id),
    )
    clear_issue(conn, context.request_id)
    advance_completed(conn, request_view(row).window.source, context.now)
    return receipt


def _failure(
    conn: sqlite3.Connection, context: CommitContext, code: str
) -> None:
    """Record rejection separately without changing a newer attempt."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            _current(conn, context)
        except CommitRejectedError:
            conn.rollback()
            return
        fail_attempt(conn, context.attempt_id, code, context.now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def commit_review(
    conn: sqlite3.Connection, context: CommitContext, result: ReviewResult
) -> Receipt:
    """Commit an entire checked review under the caller's held writer lock.

    Args:
        conn: Connection under the caller's writer lock; no active transaction.
        context: Current attempt and admitted evidence identities.
        result: Proposed content, never lifecycle instructions.

    Returns:
        The single durable success receipt, including on a repeated call.

    Raises:
        ValueError: If input or transaction ownership is invalid.
        CommitRejectedError: If state, evidence or the batch is invalid.
    """
    if conn.in_transaction:
        raise ValueError("transaction_already_active")
    context = CommitContext.model_validate_json(canonical_json(context))
    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt = _receipt(conn, context.request_id)
        if receipt is None:
            require_current_tombstones(conn, context.home)
            require_transaction(conn, context.now)
            row = _current(conn, context)
            policy = _policy(conn, row)
            if (
                context.knowledge_digest is not None
                and context.knowledge_digest
                != snapshot(conn, request_view(row).scope)
            ):
                raise CommitRejectedError("rejected_revision")
            if (
                policy.policy_id.endswith("-v3")
                and row["origin"] == "review"
                and context.knowledge_digest is None
            ):
                raise CommitRejectedError("rejected_revision")
            try:
                result = parse_result(
                    canonical_json(result).encode(), policy.max_output_bytes
                )
            except ValueError:
                raise CommitRejectedError("rejected_validation") from None
            evidence = _evidence(conn, context, row, policy)
            _batch(conn, result, request_view(row).scope, evidence)
            receipt = _complete(conn, context, row, result)
        conn.commit()
        return receipt
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, CommitRejectedError):
            _failure(conn, context, str(exc))
        raise
