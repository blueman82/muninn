"""Execute deterministic applications after scheduling releases its root."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muninn.decision_apply import enrich_support
from muninn.decision_candidates import candidate_view
from muninn.decision_models import DecisionId, DecisionIntent, DecisionView
from muninn.decision_write import decision_view, submit_intent
from muninn.review_commit import CommitContext, commit_review
from muninn.review_commit_state import EVIDENCE_ERRORS
from muninn.review_read_budget import EvidenceLimitError
from muninn.review_requests import claim_attempt
from muninn.review_supervisor_store import database


def execute_application(
    home: Path, ident: DecisionId, now: float, generation: int
) -> DecisionView:
    """Claim normal bounded work and call the single checked commit owner."""
    try:
        with database(home, transaction=False) as conn:
            view = candidate_view(conn, ident)
            if view.state != "approved_pending" or view.code is not None:
                return decision_view(conn, ident)
            row = conn.execute(
                "SELECT r.* FROM review_request r JOIN memory_candidate c "
                "ON c.application_request_id=r.id WHERE c.id=? "
                "AND r.state IN ('pending','waiting')",
                (ident,),
            ).fetchone()
            if row is None or view.candidate is None:
                return decision_view(conn, ident)
            owner = conn.execute(
                "SELECT owner_evidence_id FROM memory_decision "
                "WHERE candidate_id=? AND accepted_revision=? "
                "AND action='approve' AND state='verified'",
                (ident, view.revision),
            ).fetchone()
            if owner is None:
                return decision_view(conn, ident)
            conn.execute("BEGIN IMMEDIATE")
            attempt = claim_attempt(conn, row["id"], generation, now)
            conn.commit()
            context = CommitContext(
                home=home,
                request_id=row["id"],
                attempt_id=attempt.id,
                generation=generation,
                evidence_ids=tuple(
                    r[0]
                    for r in conn.execute(
                        "SELECT evidence_id FROM review_input "
                        "WHERE request_id=?",
                        (row["id"],),
                    )
                ),
                now=now,
            )
            commit_review(
                conn, context, enrich_support(view.candidate, owner[0])
            )
            return decision_view(conn, ident)
    except ValueError as exc:
        return _refusal(home, ident, exc)


def _refusal(home: Path, ident: DecisionId, exc: ValueError) -> DecisionView:
    """Retain finite application refusal without weakening its active fence."""
    code = exc.code if isinstance(exc, EvidenceLimitError) else str(exc)
    code = EVIDENCE_ERRORS.get(code, code)
    if code == "review_policy_required":
        code = "rejected_validation"
    if code in {
        "review_concurrency",
        "request_not_ready",
        "review_attempt_limit",
    }:
        with database(home) as conn:
            return decision_view(conn, ident)
    if code not in (
        "rejected_validation",
        "rejected_evidence",
        "rejected_scope",
        "rejected_revision",
        "rejected_authority",
        "rejected_erased",
        "rejected_fenced",
        "evidence_limit",
        "learning_paused",
        "evidence_byte_limit",
        "evidence_record_limit",
        "evidence_work_limit",
        "evidence_selection_limit",
    ):
        raise exc
    with database(home) as conn:
        conn.execute(
            "UPDATE memory_candidate SET code=? WHERE id=? "
            "AND state='approved_pending'",
            (code, ident),
        )
        return decision_view(conn, ident)


def saved_view(
    conn: sqlite3.Connection, view: DecisionView, now: float
) -> DecisionView:
    """Return the receipt and retire losing intent without acceptance."""
    if view.receipt is None:
        raise ValueError("decision_invalid")
    row = conn.execute(
        "SELECT locator_json FROM memory_decision WHERE candidate_id=? "
        "AND accepted_revision IS NULL AND state='waiting_verification' "
        "ORDER BY submitted_at DESC,id DESC LIMIT 1",
        (view.id,),
    ).fetchone()
    if row is not None:
        return submit_intent(
            conn, DecisionIntent.model_validate_json(row[0]), now
        )
    return view
