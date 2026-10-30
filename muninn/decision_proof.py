"""Verify whole original owner decisions and prior complete presentation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from pydantic import TypeAdapter

from muninn.decision_candidates import candidate_view, show_candidate
from muninn.decision_models import (
    Candidate,
    DecisionCode,
    DecisionIntent,
    DecisionView,
    VerifiedDecision,
)
from muninn.knowledge_scope import scope_applies
from muninn.review_authority import injected
from muninn.review_commit_state import EVIDENCE_ERRORS
from muninn.review_evidence import validate_evidence
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    EvidenceWindow,
    SourceRef,
)
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_originals import originals
from muninn.review_progress import source_row
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_selectors import (
    build_record,
    decode,
    fields,
    selected,
)
from muninn.tombstones import require_current_tombstones


def decision_words(intent: DecisionIntent) -> str:
    """Return the supported dedicated whole-field instruction."""
    if intent.action == "edit":
        return (
            f"Please replace memory {intent.candidate_id} "
            f"with memory {intent.replacement_id}."
        )
    return f"Please {intent.action} memory {intent.candidate_id}."


def _original(
    conn: sqlite3.Connection,
    locator: EvidenceLocator,
    selector: EvidenceSelector,
    budget: ReadBudget,
    limit: int,
) -> EvidenceRecord:
    """Recover a checked primary original and require a complete field."""
    try:
        source = source_row(conn, locator.source)
    except ValueError:
        raise ValueError("waiting_index") from None
    if locator.line > source["cursor_line"]:
        raise ValueError("waiting_index")
    if limit <= 0:
        raise EvidenceLimitError("evidence_byte_limit")
    _, raw, physical = originals(
        conn, source, locator.line, locator.line, limit, budget=budget
    )[0]
    record = decode(raw)
    context = fields(locator.source.provider, record)
    field, _ = selected(context, locator.part, selector)
    if (
        field.evidence_type == "owner_statement"
        and sum(
            bool(item.text.strip())
            for item in context
            if item.evidence_type == "owner_statement"
        )
        != 1
    ):
        raise ValueError("owner_proof")
    if field.evidence_type == "assistant_report" and injected(record):
        raise ValueError("display_proof")
    if selector.start not in (None, 0) or selector.end not in (
        None,
        len(field.text),
    ):
        raise ValueError("owner_proof")
    return build_record(conn, source, locator, selector, raw, physical)


def _persist(conn: sqlite3.Connection, record: EvidenceRecord) -> str:
    """Retain the verified selection using a stable governed identity."""
    data = record.model_dump(mode="json")
    ident = _stable_id(record)
    data["id"] = ident
    for key in ("scope", "selector", "original_locator"):
        data[f"{key}_json"] = canonical_json_value(data.pop(key))
    conn.execute(
        f"INSERT OR IGNORE INTO review_evidence({','.join(data)}) "
        f"VALUES({','.join('?' for _ in data)})",
        tuple(data.values()),
    )
    row = conn.execute(
        "SELECT * FROM review_evidence WHERE id=?", (ident,)
    ).fetchone()
    if any(
        row[key] != value
        for key, value in data.items()
        if key != "selected_at"
    ):
        raise ValueError("owner_proof")
    return ident


def _target(conn: sqlite3.Connection, intent: DecisionIntent) -> DecisionView:
    """Require compatible immutable display identity and source order."""
    view = candidate_view(conn, intent.candidate_id)
    target = candidate_view(conn, intent.replacement_id or intent.candidate_id)
    if (
        target.candidate is None
        or view.candidate is None
        or target.candidate.scope != view.candidate.scope
    ):
        raise ValueError("display_proof")
    if intent.action == "edit" and target.state not in (
        "waiting_verification",
        "blocked",
    ):
        raise ValueError("display_proof")
    if (
        intent.owner_locator.source != intent.display_locator.source
        or intent.display_locator.line >= intent.owner_locator.line
    ):
        raise ValueError("display_proof")
    return target


def _remaining(policy: ReviewPolicy, start: int, budget: ReadBudget) -> int:
    """Bound cumulative physical reads by both policy and remaining grants."""
    remaining = min(
        policy.max_input_bytes - (start - budget.bytes_left), budget.bytes_left
    )
    if remaining <= 0:
        raise EvidenceLimitError("evidence_byte_limit")
    return remaining


def _supports(
    conn: sqlite3.Connection,
    home: Path,
    candidates: tuple[Candidate, ...],
    policy: ReviewPolicy,
    budget: ReadBudget,
    start: int,
) -> None:
    """Recheck frozen factual supports without granting another allowance."""
    for candidate in candidates:
        remaining = _remaining(policy, start, budget)
        validate_evidence(
            conn,
            home,
            candidate.original_evidence_ids,
            candidate.scope,
            policy.model_copy(update={"max_input_bytes": remaining}),
            budget=budget,
        )


def _stable_id(record: EvidenceRecord) -> str:
    """Derive the exact persisted identity without writing evidence."""
    identity = record.model_dump(exclude={"id", "selected_at"}, mode="json")
    digest = hashlib.sha256(
        canonical_json_value(identity).encode()
    ).hexdigest()
    return f"E_{digest[:32]}"


def _retained(conn: sqlite3.Connection, record: EvidenceRecord) -> None:
    """Require reused metadata to remain the exact retained live selection."""
    row = conn.execute(
        "SELECT * FROM review_evidence WHERE id=?", (record.id,)
    ).fetchone()
    data = record.model_dump(mode="json")
    if row is None:
        raise ValueError("owner_proof")
    retained = dict(row)
    for key in ("scope", "selector", "original_locator"):
        retained[key] = json.loads(retained.pop(f"{key}_json"))
    if any(
        retained[key] != value
        for key, value in data.items()
        if key != "selected_at"
    ):
        raise ValueError("owner_proof")


def _reuse(
    conn: sqlite3.Connection,
    evidence: tuple[EvidenceRecord, ...],
    candidates: tuple[Candidate, ...],
    owner: EvidenceRecord,
    display: EvidenceRecord,
) -> None:
    """Bind fresh admitted records to complete proof and original supports."""
    records = {record.id: record for record in evidence}
    required = {
        ref
        for candidate in candidates
        for ref in candidate.original_evidence_ids
    } | {_stable_id(owner), _stable_id(display)}
    if (
        len(records) != len(evidence)
        or set(records) != required
        or any(record.state != "live" for record in evidence)
    ):
        raise ValueError("owner_proof")
    for record in evidence:
        _retained(conn, record)
        source_row(
            conn,
            SourceRef(provider=record.provider, thread_id=record.thread_id),
        )
    for fresh in (owner, display):
        retained = records[_stable_id(fresh)]
        if fresh.model_dump(
            exclude={"id", "selected_at"}
        ) != retained.model_dump(exclude={"id", "selected_at"}):
            raise ValueError("owner_proof")


def _checked_supports(
    conn: sqlite3.Connection,
    home: Path,
    candidates: tuple[Candidate, ...],
    policy: ReviewPolicy,
    budget: ReadBudget,
    start: int,
    selections: tuple[
        tuple[EvidenceRecord, ...] | None, EvidenceRecord, EvidenceRecord
    ],
) -> None:
    """Reuse commit records or physically validate standalone supports."""
    evidence, owner, display = selections
    if evidence is None:
        _supports(conn, home, candidates, policy, budget, start)
    else:
        _reuse(conn, evidence, candidates, owner, display)


def verify_decision(
    conn: sqlite3.Connection,
    home: Path,
    intent: DecisionIntent,
    policy: ReviewPolicy,
    budget: ReadBudget,
    *,
    from_evidence: tuple[EvidenceRecord, ...] | None = None,
) -> VerifiedDecision:
    """Verify originals and retain actual evidence in the caller transaction.

    Args:
        conn: Caller-owned write transaction; no lock or transaction is opened.
        home: Store home for current tombstone checks.
        intent: Locator-only action, revalidated without trusting caller flags.
        policy: Operation projection, scope and total input bounds.
        budget: The single operation allowance, including remaining charges.
        from_evidence: Actual records freshly revalidated by the commit owner.

    Returns:
        Internal proof referencing actual persisted original selections.

    Raises:
        ValueError: If complete presentation, human intent or provenance fails.
        EvidenceLimitError: If the shared physical read allowance is exhausted.

    Evidence is inserted only after every check succeeds. The caller must roll
    back the transaction on failure. No decision or candidate state is changed.
    """
    try:
        return _verify(
            conn, home, intent, policy, budget, from_evidence=from_evidence
        )
    except EvidenceLimitError:
        raise
    except ValueError as exc:
        code = EVIDENCE_ERRORS.get(str(exc), str(exc))
        if code == "erasure_pending":
            code = "rejected_erased"
        try:
            TypeAdapter(DecisionCode).validate_python(code)
        except ValueError:
            code = "decision_invalid"
        raise ValueError(code) from None


def _verify(
    conn: sqlite3.Connection,
    home: Path,
    intent: DecisionIntent,
    policy: ReviewPolicy,
    budget: ReadBudget,
    *,
    from_evidence: tuple[EvidenceRecord, ...] | None = None,
) -> VerifiedDecision:
    """Check all proof predicates before retaining governed selections."""
    if not conn.in_transaction:
        raise ValueError("transaction")
    intent = DecisionIntent.model_validate_json(canonical_json(intent))
    view = candidate_view(conn, intent.candidate_id)
    if view.state == "saved":
        raise ValueError("already_saved")
    if view.candidate is None:
        raise ValueError("decision_erased")
    require_current_tombstones(conn, home)
    target = _target(conn, intent)
    start = budget.bytes_left
    owner = _original(
        conn,
        intent.owner_locator,
        intent.owner_selector,
        budget,
        policy.max_input_bytes,
    )
    if owner.excerpt == "approved":
        raise ValueError("decision_reference_required")
    if (
        owner.evidence_type != "owner_statement"
        or owner.authority != "direct_owner"
    ):
        raise ValueError("owner_proof")
    if owner.excerpt != decision_words(intent):
        raise ValueError("owner_intent")
    display = _original(
        conn,
        intent.display_locator,
        intent.display_selector,
        budget,
        _remaining(policy, start, budget),
    )
    if (
        display.evidence_type != "assistant_report"
        or display.excerpt
        != show_candidate(conn, target.id, policy.context_bytes).text
    ):
        raise ValueError("display_proof")
    if not all(
        scope_applies(record.scope, view.candidate.scope)
        for record in (owner, display)
    ):
        raise ValueError("rejected_scope")
    candidates = (view.candidate,)
    if intent.replacement_id is not None and target.candidate is not None:
        candidates += (target.candidate,)
    _checked_supports(
        conn,
        home,
        candidates,
        policy,
        budget,
        start,
        (from_evidence, owner, display),
    )
    return VerifiedDecision(
        candidate_id=view.id,
        candidate_digest=view.digest,
        action=intent.action,
        owner_evidence_id=_persist(conn, owner),
        display_evidence_id=_persist(conn, display),
        owner_window=EvidenceWindow(
            source=intent.owner_locator.source,
            from_line=owner.line,
            through_line=owner.line,
            boundary_hash=owner.line_sha256,
        ),
        replacement_id=intent.replacement_id,
        replacement_digest=target.digest if intent.replacement_id else None,
    )
