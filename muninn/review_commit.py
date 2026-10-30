"""Apply a review result under one checked, idempotent SQLite transaction."""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from muninn.decision_apply import finish_application, prepare_application
from muninn.knowledge_models import ReviewResult
from muninn.knowledge_write import (
    apply_change,
    json_value,
    save_unresolved,
)
from muninn.redaction import redact
from muninn.review_candidates import (
    CommitRejectedError as CommitRejectedError,
)
from muninn.review_candidates import (
    ProposalRejectedError,
    validate_batch,
)
from muninn.review_commit_state import (
    EVIDENCE_ERRORS as EVIDENCE_ERRORS,
)
from muninn.review_commit_state import (
    CommitContext as CommitContext,
)
from muninn.review_commit_state import (
    checked_request_policy as checked_request_policy,
)
from muninn.review_commit_state import (
    stored_receipt as stored_receipt,
)
from muninn.review_evidence import validate_evidence
from muninn.review_evidence_models import EvidenceRecord
from muninn.review_json import canonical_json, parse_result
from muninn.review_models import Receipt, ReviewPolicy
from muninn.review_progress import (
    advance_completed,
    check_window,
    require_transaction,
)
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_recovery import clear as clear_issue
from muninn.review_requests import fail_attempt, request_view
from muninn.review_snapshot import snapshot
from muninn.tombstones import require_current_tombstones


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


def _evidence(
    conn: sqlite3.Connection,
    context: CommitContext,
    row: sqlite3.Row,
    policy: ReviewPolicy,
    *,
    budget: ReadBudget,
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
        raise CommitRejectedError("rejected_evidence", "original evidence")
    view = request_view(row)
    try:
        source, _ = check_window(
            conn, view.window, view.scope, policy, budget=budget
        )
        if source["session_root"] != row["session_root"]:
            raise CommitRejectedError("rejected_evidence", "original evidence")
        records = validate_evidence(
            conn, context.home, ids, view.scope, policy, budget=budget
        )
    except ValidationError:
        raise CommitRejectedError(
            "rejected_evidence", "original evidence"
        ) from None
    except EvidenceLimitError as exc:
        raise CommitRejectedError(exc.code, "original evidence") from None
    except ValueError as exc:
        code = EVIDENCE_ERRORS.get(str(exc))
        if code is None:
            raise
        raise CommitRejectedError(code, "original evidence") from None
    return records


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
    conn: sqlite3.Connection,
    context: CommitContext,
    result: ReviewResult,
    *,
    defer_candidate_failure: bool = False,
) -> Receipt:
    """Commit an entire checked review under the caller's held writer lock.

    Args:
        conn: Connection under the caller's writer lock; no active transaction.
        context: Current attempt and admitted evidence identities.
        result: Proposed content, never lifecycle instructions.
        defer_candidate_failure: Leave eligible candidate refusals unfinished.

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
        receipt = stored_receipt(conn, context.request_id)
        if receipt is None:
            require_current_tombstones(conn, context.home)
            require_transaction(conn, context.now)
            row = _current(conn, context)
            policy = checked_request_policy(conn, row)
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
            budget = ReadBudget(policy.max_input_bytes)
            evidence = _evidence(conn, context, row, policy, budget=budget)
            fence = prepare_application(
                conn, context, result, policy, evidence, budget
            )
            validate_batch(
                conn,
                result,
                request_view(row).scope,
                evidence,
                defer_candidate_failure=defer_candidate_failure,
            )
            receipt = _complete(conn, context, row, result)
            if fence is not None:
                finish_application(conn, fence, receipt, context.now)
        conn.commit()
        return receipt
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, ProposalRejectedError) and defer_candidate_failure:
            raise
        if isinstance(exc, CommitRejectedError):
            _failure(conn, context, str(exc))
        raise
