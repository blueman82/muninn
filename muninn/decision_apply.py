"""Fence exact memory applications inside the normal commit transaction."""

from __future__ import annotations

import hashlib
import sqlite3

from muninn.decision_candidates import candidate_view
from muninn.decision_identity import application_key
from muninn.decision_models import Candidate, DecisionFence, DecisionIntent
from muninn.decision_proof import verify_decision
from muninn.decision_read import show_unresolved
from muninn.knowledge_models import EvidenceId, ReviewResult
from muninn.knowledge_write import body_of, redacted_body, target
from muninn.redaction import redact, redact_parts
from muninn.review_candidates import CommitRejectedError
from muninn.review_commit_state import CommitContext
from muninn.review_evidence_models import EvidenceRecord
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import Receipt, ReviewPolicy
from muninn.review_progress import require_transaction
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_request_views import request_view


def enrich_support(candidate: Candidate, owner_id: EvidenceId) -> ReviewResult:
    """Append real owner support once without changing any other field."""
    data = candidate.result.model_dump(mode="json")
    for change in data["changes"]:
        supported = (
            [change]
            if change["form"] == "note"
            else [
                change["summary"],
                *change["applicability"],
                *(step["claim"] for step in change["steps"]),
                *change["decision_points"],
                *change["pitfalls"],
            ]
        )
        for claim in supported:
            if owner_id not in claim["evidence_refs"]:
                claim["evidence_refs"].append(owner_id)
    return ReviewResult.model_validate_json(canonical_json_value(data))


def _binding(
    conn: sqlite3.Connection, context: CommitContext
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """Require the immutable application pair and active accepted approval."""
    row = conn.execute(
        "SELECT * FROM review_request WHERE id=?", (context.request_id,)
    ).fetchone()
    if row["candidate_id"] is None and row["decision_revision"] is None:
        return None
    decision = conn.execute(
        "SELECT d.* FROM memory_candidate c JOIN memory_decision d "
        "ON d.id=c.active_decision_id AND d.candidate_id=c.id "
        "AND d.candidate_digest=c.digest AND d.accepted_revision=c.revision "
        "WHERE c.id=? AND c.state='approved_pending' AND c.revision=? "
        "AND c.application_request_id=? AND d.state='verified' "
        "AND d.action='approve'",
        (row["candidate_id"], row["decision_revision"], context.request_id),
    ).fetchone()
    if decision is None or row["origin"] != "proposal":
        raise CommitRejectedError("rejected_fenced")
    return row, decision


def _current_content(conn: sqlite3.Connection, candidate: Candidate) -> None:
    """Refuse changed normalizers or targets before writing approved data."""
    for change in candidate.result.changes:
        body = body_of(change)
        if body != redacted_body(body):
            raise CommitRejectedError("rejected_validation")
    for item in candidate.result.unresolved:
        if [item.claim, item.question] != redact_parts(
            [item.claim, item.question]
        ):
            raise CommitRejectedError("rejected_validation")
    disposition = candidate.disposition
    if (
        disposition is not None
        and redact(disposition.answer)[0] != disposition.answer
    ):
        raise CommitRejectedError("rejected_validation")
    for ref in candidate.targets:
        try:
            target(conn, ref, candidate.scope)
        except ValueError as exc:
            raise CommitRejectedError(str(exc)) from None


def prepare_application(
    conn: sqlite3.Connection,
    context: CommitContext,
    result: ReviewResult,
    policy: ReviewPolicy,
    evidence: tuple[EvidenceRecord, ...],
    budget: ReadBudget,
) -> DecisionFence | None:
    """Recheck exact approval and derived content with the remaining budget."""
    require_transaction(conn, context.now)
    binding = _binding(conn, context)
    if binding is None:
        return None
    row, decision = binding
    view = candidate_view(conn, row["candidate_id"])
    candidate = view.candidate
    if candidate is None:
        raise CommitRejectedError("rejected_erased")
    if (
        row["duplicate_key"]
        != application_key(view.id, view.digest, view.revision)
        or row["scope_json"] != canonical_json(candidate.scope)
        or {record.id for record in evidence}
        != {
            *candidate.original_evidence_ids,
            decision["owner_evidence_id"],
            decision["display_evidence_id"],
        }
    ):
        raise CommitRejectedError("rejected_fenced")
    _current_content(conn, candidate)
    try:
        proof = verify_decision(
            conn,
            context.home,
            DecisionIntent.model_validate_json(decision["locator_json"]),
            policy,
            budget,
            from_evidence=evidence,
        )
    except EvidenceLimitError as exc:
        raise CommitRejectedError(exc.code) from None
    except ValueError:
        raise CommitRejectedError("rejected_evidence") from None
    if (
        proof.action != "approve"
        or proof.candidate_digest != view.digest
        or proof.candidate_id != view.id
        or proof.owner_evidence_id != decision["owner_evidence_id"]
        or proof.display_evidence_id != decision["display_evidence_id"]
        or request_view(row).window != proof.owner_window
    ):
        raise CommitRejectedError("rejected_fenced")
    expected = enrich_support(candidate, proof.owner_evidence_id)
    if canonical_json(expected) != canonical_json(result):
        raise CommitRejectedError("rejected_validation")
    return DecisionFence(
        candidate_id=view.id,
        candidate_digest=view.digest,
        revision=view.revision,
        owner_evidence_id=proof.owner_evidence_id,
        derived_digest=hashlib.sha256(
            canonical_json(expected).encode()
        ).hexdigest(),
    )


def _resolve(
    conn: sqlite3.Connection, candidate: Candidate, receipt: Receipt
) -> None:
    """Resolve only a complete displayed answer covering current targets."""
    disposition = candidate.disposition
    if disposition is None or not disposition.complete:
        return
    question = show_unresolved(conn, disposition.unresolved_id)
    if (
        question.state != "open"
        or question.scope != candidate.scope
        or question.item is None
    ):
        return
    replaced = {
        (change.knowledge_id, change.expected_revision)
        for change in candidate.result.changes
        if change.action == "replace"
        and any(
            item.knowledge_id == change.knowledge_id
            for item in receipt.applied
        )
    }
    required = {
        (ref.knowledge_id, ref.revision)
        for ref in question.affected
        if ref not in question.stale_affected
        or (ref.knowledge_id, ref.revision) in replaced
    }
    covered = {
        (ref.knowledge_id, ref.revision) for ref in disposition.affected
    }
    if not required <= covered:
        return
    changed = conn.execute(
        "UPDATE unresolved SET state='resolved' WHERE id=? AND state='open'",
        (question.id,),
    )
    if changed.rowcount != 1:
        raise CommitRejectedError("rejected_fenced")


def finish_application(
    conn: sqlite3.Connection,
    fence: DecisionFence,
    receipt: Receipt,
    now: float,
) -> None:
    """Save outcome and justified resolution before the caller commits."""
    require_transaction(conn, now)
    view = candidate_view(conn, fence.candidate_id)
    decision = conn.execute(
        "SELECT id FROM memory_decision WHERE candidate_id=? "
        "AND accepted_revision=? AND candidate_digest=? AND action='approve' "
        "AND state='verified' AND owner_evidence_id=?",
        (
            fence.candidate_id,
            fence.revision,
            fence.candidate_digest,
            fence.owner_evidence_id,
        ),
    ).fetchone()
    if decision is None or view.candidate is None:
        raise CommitRejectedError("rejected_fenced")
    changed = conn.execute(
        "UPDATE memory_candidate SET state='saved',derived_digest=?,code=NULL "
        "WHERE id=? AND digest=? AND revision=? AND state='approved_pending' "
        "AND active_decision_id=? AND application_request_id=?",
        (
            fence.derived_digest,
            fence.candidate_id,
            fence.candidate_digest,
            fence.revision,
            decision["id"],
            receipt.request_id,
        ),
    )
    if changed.rowcount != 1:
        raise CommitRejectedError("rejected_fenced")
    _resolve(conn, view.candidate, receipt)
