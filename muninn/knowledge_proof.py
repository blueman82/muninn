"""Show agents the owner's own words in place of the model's claim text."""

from __future__ import annotations

import json
import sqlite3

from muninn.knowledge_models import (
    EvidenceId,
    KnowledgeView,
    NoteBody,
    ReviewResult,
    SupportedClaim,
)
from muninn.knowledge_write import body_of, claims

PROOF_LIMIT = 2000
_SPEAKER = {"owner_statement": "owner", "assistant_report": "assistant"}


def proof_text(conn: sqlite3.Connection, refs: list[EvidenceId]) -> str | None:
    """Return the cited full owner and paired reply text.

    Args:
        conn: Readable store connection.
        refs: Evidence references of one claim.

    Returns:
        The full redacted messages in transcript order, each marked with its
        speaker, or None when no full owner message is cited.
    """
    marks = ",".join("?" for _ in refs)
    rows = conn.execute(
        "SELECT evidence_type,authority,selector_json,excerpt FROM "
        f"review_evidence WHERE id IN ({marks}) AND state='live' "
        "AND excerpt IS NOT NULL ORDER BY provider,thread_id,line,part",
        refs,
    ).fetchall()
    whole = [
        row
        for row in rows
        if json.loads(row["selector_json"]).get("start") is None
        and json.loads(row["selector_json"]).get("end") is None
    ]
    if not any(
        row["evidence_type"] == "owner_statement"
        and row["authority"] == "direct_owner"
        for row in whole
    ):
        return None
    return "\n\n".join(
        f"[{_SPEAKER[row['evidence_type']]}] {row['excerpt']}"
        for row in whole
        if row["evidence_type"] in _SPEAKER
    )


def _proof(
    conn: sqlite3.Connection, refs: list[EvidenceId], ident: str
) -> str:
    """Return the proof, or a look-up line when it is missing or too long."""
    text = proof_text(conn, refs)
    # Fail closed: without its proof the model text is not shown either.
    if text is None:
        return f"proof unavailable: run muninn know show {ident}"
    # A cut proof could lose a condition, so a long one is not shown.
    if len(text.encode()) > PROOF_LIMIT:
        return f"proof too long: run muninn know show {ident}"
    return text


def _approved(conn: sqlite3.Connection, view: KnowledgeView) -> bool:
    """Report whether the owner approved this exact text (ADR 0018)."""
    row = conn.execute(
        "SELECT r.candidate_id FROM knowledge_version v JOIN review_request r "
        "ON r.id=v.request_id WHERE v.knowledge_id=? AND v.revision=?",
        (view.id, view.revision),
    ).fetchone()
    return row is not None and row[0] is not None


def _owner_required(view: KnowledgeView) -> bool:
    """Report whether the knowledge needed owner support."""
    body = view.body
    return body is not None and (
        body.kind in ("preference", "decision")
        or body.scope.project_id is None
    )


def owner_proofs(conn: sqlite3.Connection, view: KnowledgeView) -> list[str]:
    """Return the full proofs to print beside the model text for the owner.

    Args:
        conn: Readable store connection.
        view: A knowledge view as stored.

    Returns:
        One full proof for each claim with one, without the agent cap.
    """
    body = view.body
    if body is None or not _owner_required(view):
        return []
    refs = (
        [body.evidence_refs]
        if isinstance(body, NoteBody)
        else [ref for _, _, ref, _ in claims(body)]
    )
    found = (proof_text(conn, ref) for ref in refs)
    return list(dict.fromkeys(text for text in found if text is not None))


def proof_view(conn: sqlite3.Connection, view: KnowledgeView) -> KnowledgeView:
    """Replace owner-supported claim text with its proof for agent paths.

    Args:
        conn: Readable store connection inside the caller's snapshot.
        view: A current knowledge view as stored.

    Returns:
        For a preference, a decision or global knowledge, the view with each
        claim showing its full cited owner message and any paired assistant
        reply instead of the model's text, or a look-up line when that proof
        is missing or too long. Facts and procedures, and text the owner
        approved through an ADR 0018 decision, keep the model's text.
    """
    body = view.body
    # Only owner-required knowledge has an owner proof to show.
    if body is None or not _owner_required(view) or _approved(conn, view):
        return view
    if isinstance(body, NoteBody):
        proof = _proof(conn, body.evidence_refs, view.id)
        clean = body.model_copy(update={"content": proof, "conditions": []})
        return view.model_copy(update={"body": clean})

    def shown(claim: SupportedClaim) -> SupportedClaim:
        proof = _proof(conn, claim.evidence_refs, view.id)
        return claim.model_copy(update={"text": proof})

    guide = body.model_copy(
        update={
            "summary": shown(body.summary),
            "applicability": [shown(c) for c in body.applicability],
            "decision_points": [shown(c) for c in body.decision_points],
            "pitfalls": [shown(c) for c in body.pitfalls],
            "steps": [
                step.model_copy(update={"claim": shown(step.claim)})
                for step in body.steps
            ],
        }
    )
    return view.model_copy(update={"body": guide})


def candidate_proofs(
    conn: sqlite3.Connection, result: ReviewResult
) -> list[str]:
    """Return the full proofs cited by a staged candidate, for the owner.

    Args:
        conn: Readable store connection.
        result: The candidate's frozen changes.

    Returns:
        One full proof for each distinct cited owner message and reply.
    """
    found = (
        proof_text(conn, refs)
        for change in result.changes
        for _, _, refs, _ in claims(body_of(change))
    )
    return list(dict.fromkeys(text for text in found if text is not None))
