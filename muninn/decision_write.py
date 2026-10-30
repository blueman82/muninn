"""Retain locator intent and immutable, monotonic accepted decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

from muninn.decision_candidates import candidate_view, show_candidate
from muninn.decision_models import (
    DecisionIntent,
    DecisionView,
    VerifiedDecision,
)
from muninn.decision_proof import decision_words
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import require_transaction, source_row
from muninn.review_selectors import check_tombstones


def _key(intent: DecisionIntent, digest: str) -> str:
    """Bind immutable submission identity to the complete frozen candidate."""
    return hashlib.sha256(
        f"{digest}\n{canonical_json(intent)}".encode()
    ).hexdigest()


def decision_view(conn: sqlite3.Connection, ident: str) -> DecisionView:
    """Report a pending refusal separately without replacing actual state."""
    view = candidate_view(conn, ident)
    row = conn.execute(
        "SELECT code FROM memory_decision WHERE candidate_id=? "
        "ORDER BY submitted_at DESC,id DESC LIMIT 1",
        (ident,),
    ).fetchone()
    if row and row[0] is not None:
        return DecisionView.model_validate_json(
            canonical_json(view.model_copy(update={"code": row[0]}))
        )
    return view


def _saved(conn: sqlite3.Connection, ident: str) -> DecisionView:
    """Retain losing staged intents without any accepted proof or revision."""
    conn.execute(
        "UPDATE memory_decision SET state='blocked',code='already_saved' "
        "WHERE candidate_id=? AND accepted_revision IS NULL "
        "AND state!='erased'",
        (ident,),
    )
    return decision_view(conn, ident)


def submit_intent(
    conn: sqlite3.Connection, intent: DecisionIntent, now: float
) -> DecisionView:
    """Stage locator-only intent idempotently inside the caller transaction."""
    require_transaction(conn, now)
    intent = DecisionIntent.model_validate_json(canonical_json(intent))
    view = candidate_view(conn, intent.candidate_id)
    if view.state == "saved":
        return _saved(conn, view.id)
    if view.candidate is None:
        raise ValueError("decision_erased")
    if intent.replacement_id:
        replacement = candidate_view(conn, intent.replacement_id)
        if (
            replacement.candidate is None
            or replacement.candidate.scope != view.candidate.scope
        ):
            raise ValueError("decision_invalid")
    conn.execute(
        "INSERT OR IGNORE INTO memory_decision(id,candidate_id,"
        "candidate_digest,"
        "intent_key,action,replacement_candidate_id,locator_json,"
        "submitted_at,state) "
        "VALUES(?,?,?,?,?,?,?,?,'waiting_verification')",
        (
            f"D_{uuid4().hex}",
            view.id,
            view.digest,
            _key(intent, view.digest),
            intent.action,
            intent.replacement_id,
            canonical_json(intent),
            now,
        ),
    )
    return decision_view(conn, view.id)


def _record(conn: sqlite3.Connection, ident: str) -> EvidenceRecord:
    """Load a strict live evidence record without manufacturing provenance."""
    row = conn.execute(
        "SELECT * FROM review_evidence WHERE id=?", (ident,)
    ).fetchone()
    if row is None:
        raise ValueError("owner_proof")
    data = dict(row)
    for key in ("scope", "selector", "original_locator"):
        data[key] = json.loads(data.pop(f"{key}_json"))
    record = EvidenceRecord.model_validate_json(json.dumps(data))
    if record.state != "live":
        raise ValueError("owner_proof")
    source = source_row(
        conn,
        SourceRef(
            provider=record.provider,
            thread_id=record.thread_id,
        ),
    )
    check_tombstones(
        conn,
        source,
        EvidenceLocator(
            source=SourceRef(
                provider=record.provider, thread_id=record.thread_id
            ),
            line=record.line,
            part=record.part,
            line_sha256=record.line_sha256,
        ),
    )
    return record


def _matches(
    record: EvidenceRecord, intent: DecisionIntent, *, owner: bool
) -> bool:
    """Bind accepted evidence to the exact immutable locator and selector."""
    locator = intent.owner_locator if owner else intent.display_locator
    selector = intent.owner_selector if owner else intent.display_selector
    return (
        record.provider,
        record.thread_id,
        record.line,
        record.part,
        record.line_sha256,
        record.selector,
    ) == (
        locator.source.provider,
        locator.source.thread_id,
        locator.line,
        locator.part,
        locator.line_sha256,
        selector,
    )


def _checked_intent(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    proof: VerifiedDecision,
    context_bytes: int,
) -> DecisionIntent:
    """Check retained physical proof bindings before accepting a revision."""
    intent = DecisionIntent.model_validate_json(row["locator_json"])
    owner, display = _record(conn, proof.owner_evidence_id), _record(
        conn, proof.display_evidence_id
    )
    target = candidate_view(conn, intent.replacement_id or intent.candidate_id)
    if (
        row["intent_key"] != _key(intent, proof.candidate_digest)
        or intent.action != proof.action
        or intent.replacement_id != proof.replacement_id
        or not _matches(owner, intent, owner=True)
        or not _matches(display, intent, owner=False)
        or owner.authority != "direct_owner"
        or owner.evidence_type != "owner_statement"
        or owner.excerpt != decision_words(intent)
        or display.evidence_type != "assistant_report"
        or display.excerpt
        != show_candidate(conn, target.id, context_bytes).text
        or proof.owner_window.source != intent.owner_locator.source
        or proof.owner_window.from_line != owner.line
        or proof.owner_window.through_line != owner.line
        or proof.owner_window.boundary_hash != owner.line_sha256
        or (target.digest if intent.replacement_id else None)
        != proof.replacement_digest
    ):
        raise ValueError("owner_proof")
    return intent


def _submitted(
    conn: sqlite3.Connection, proof: VerifiedDecision
) -> sqlite3.Row:
    """Resolve exactly one submitted identity without scanning history."""
    owner = _record(conn, proof.owner_evidence_id)
    display = _record(conn, proof.display_evidence_id)

    def locator(record: EvidenceRecord) -> EvidenceLocator:
        return EvidenceLocator(
            source=SourceRef(
                provider=record.provider, thread_id=record.thread_id
            ),
            line=record.line,
            part=record.part,
            line_sha256=record.line_sha256,
        )

    intent = DecisionIntent(
        candidate_id=proof.candidate_id,
        action=proof.action,
        owner_locator=locator(owner),
        owner_selector=owner.selector,
        display_locator=locator(display),
        display_selector=display.selector,
        replacement_id=proof.replacement_id,
    )
    row = conn.execute(
        "SELECT * FROM memory_decision WHERE intent_key=?",
        (_key(intent, proof.candidate_digest),),
    ).fetchone()
    if row is None or row["state"] == "erased":
        raise ValueError("owner_proof")
    return row


def accept_decision(
    conn: sqlite3.Connection,
    proof: VerifiedDecision,
    now: float,
    *,
    policy: ReviewPolicy,
) -> DecisionView:
    """Advance one genuinely verified decision in the caller transaction.

    Args:
        conn: Caller-owned transaction; the caller rolls back any failure.
        proof: Internal physical-verifier result, reverified again at apply.
        now: Finite nonnegative acceptance timestamp.
        policy: Actual current checked policy, including presentation ceiling.

    Returns:
        Actual candidate status and any existing immutable saved receipt.

    Raises:
        ValueError: If proof, current identity or transaction is invalid.
    """
    require_transaction(conn, now)
    proof = VerifiedDecision.model_validate_json(canonical_json(proof))
    view = candidate_view(conn, proof.candidate_id)
    if view.state == "saved":
        return _saved(conn, view.id)
    policy = ReviewPolicy.model_validate_json(canonical_json(policy))
    if view.state in ("rejected", "superseded", "erased"):
        return decision_view(conn, view.id)
    if view.digest != proof.candidate_digest:
        raise ValueError("candidate_changed")
    row = _submitted(conn, proof)
    if row["accepted_revision"] is not None:
        return decision_view(conn, view.id)
    intent = _checked_intent(conn, row, proof, policy.context_bytes)
    revision = view.revision + 1
    conn.execute(
        "UPDATE memory_decision SET state='verified',accepted_revision=?,"
        "owner_evidence_id=?,display_evidence_id=?,accepted_at=?,code=NULL "
        "WHERE id=?",
        (
            revision,
            proof.owner_evidence_id,
            proof.display_evidence_id,
            now,
            row["id"],
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO memory_candidate_support VALUES(?,?,?)",
        [
            (view.id, proof.owner_evidence_id, "owner"),
            (
                intent.replacement_id or view.id,
                proof.display_evidence_id,
                "display",
            ),
        ],
    )
    state = {
        "approve": "approved_pending",
        "reject": "rejected",
        "edit": "superseded",
    }[proof.action]
    changed = conn.execute(
        "UPDATE memory_candidate SET state=?,revision=?,active_decision_id=?,"
        "code=NULL WHERE id=? AND revision=?",
        (state, revision, row["id"], view.id, view.revision),
    )
    if changed.rowcount != 1:
        raise ValueError("rejected_fenced")
    return decision_view(conn, view.id)
