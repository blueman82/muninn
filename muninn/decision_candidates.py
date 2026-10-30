"""Stage normalized exact candidates and verify complete durable displays."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from muninn.decision_models import (
    Candidate,
    CandidateInput,
    DecisionDisplay,
    DecisionId,
    DecisionView,
    QuestionDisposition,
)
from muninn.decision_read import show_unresolved
from muninn.knowledge_models import (
    AffectedRevision,
    EvidenceId,
    ReviewResult,
    Scope,
)
from muninn.knowledge_write import body_of, claims, redacted_body, target
from muninn.redaction import redact, redact_parts
from muninn.review_candidates import validate_batch
from muninn.review_commit_state import (
    EVIDENCE_ERRORS,
    checked_request_policy,
    stored_receipt,
)
from muninn.review_evidence import validate_evidence
from muninn.review_evidence_models import EvidenceRecord
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_progress import check_window, require_transaction
from muninn.review_read_budget import EvidenceLimitError, ReadBudget
from muninn.review_requests import request_view
from muninn.tombstones import require_current_tombstones

ENRICHMENT_RULE = (
    "Append the verified owner evidence ID to every applicable claim support, "
    "only when absent; preserve original order and all other fields."
)


def candidate_digest(value: dict[str, Any]) -> str:
    """Hash framed canonical fields and ID under the candidate domain."""
    fields = {**value, "digest": "0" * 64}
    result = hashlib.sha256()
    for part in (
        b"muninn-memory-candidate-v1",
        canonical_json_value(fields).encode(),
    ):
        result.update(len(part).to_bytes(8, "big"))
        result.update(part)
    return result.hexdigest()


def _normalize(result: ReviewResult) -> ReviewResult:
    """Use the application normalizer before freezing any proposed content."""
    data = result.model_dump(mode="json")
    for change, encoded in zip(result.changes, data["changes"], strict=True):
        encoded.update(redacted_body(body_of(change)).model_dump(mode="json"))
    for item in data["unresolved"]:
        item["claim"], item["question"] = redact_parts(
            [item["claim"], item["question"]]
        )
    return ReviewResult.model_validate_json(canonical_json_value(data))


def _references(
    result: ReviewResult,
) -> tuple[tuple[EvidenceId, ...], tuple[AffectedRevision, ...]]:
    """Derive ordered original supports and current targets from typed data."""
    supports: list[EvidenceId] = []
    targets: list[AffectedRevision] = []
    for change in result.changes:
        for _, _, refs, _ in claims(body_of(change)):
            supports.extend(ref for ref in refs if ref not in supports)
        if change.action == "replace":
            targets.append(
                AffectedRevision(
                    knowledge_id=change.knowledge_id,
                    revision=change.expected_revision,
                )
            )
    for item in result.unresolved:
        supports.extend(
            ref for ref in item.evidence_refs if ref not in supports
        )
        targets.extend(item.affected)
    return tuple(supports), tuple(targets)


def _originals(
    conn: sqlite3.Connection,
    home: Path,
    row: sqlite3.Row,
    ids: tuple[EvidenceId, ...],
) -> tuple[EvidenceRecord, ...]:
    """Check admitted originals and the preparation window under one budget."""
    policy = checked_request_policy(conn, row)
    admitted = {
        r[0]
        for r in conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (row["id"],),
        )
    }
    if not set(ids) <= admitted:
        raise ValueError("rejected_evidence")
    view = request_view(row)
    budget = ReadBudget(policy.max_input_bytes)
    try:
        source, _ = check_window(
            conn, view.window, view.scope, policy, budget=budget
        )
        if source["session_root"] != row["session_root"]:
            raise ValueError("rejected_evidence")
        return validate_evidence(
            conn, home, ids, view.scope, policy, budget=budget
        )
    except EvidenceLimitError as exc:
        raise ValueError(exc.code) from None
    except ValueError as exc:
        raise ValueError(
            EVIDENCE_ERRORS.get(str(exc), "rejected_evidence")
        ) from None


def _disposition(
    conn: sqlite3.Connection,
    value: QuestionDisposition | None,
    scope: Scope,
) -> QuestionDisposition | None:
    """Check the proposed answer against its question and current scope."""
    if value is None:
        return None
    view = show_unresolved(conn, value.unresolved_id)
    if view.scope != scope or view.state != "open" or view.item is None:
        raise ValueError("rejected_scope")
    if len({(r.knowledge_id, r.revision) for r in value.affected}) != len(
        value.affected
    ):
        raise ValueError("rejected_revision")
    for ref in value.affected:
        if ref not in view.affected:
            raise ValueError("rejected_revision")
        target(conn, ref, scope)
    return QuestionDisposition.model_validate_json(
        canonical_json_value(
            {
                **value.model_dump(mode="json"),
                "answer": redact(value.answer)[0],
            }
        )
    )


def stage_candidate(
    conn: sqlite3.Connection,
    home: Path,
    value: CandidateInput,
    now: float,
) -> DecisionView:
    """Freeze a complete checked draft inside the caller's transaction.

    Args:
        conn: Caller-owned connection under one writer root and transaction.
        home: Private store home used for original and tombstone checks.
        value: Complete draft bound to a checked preparation request.
        now: Finite nonnegative floating-point timestamp.

    Returns:
        The new candidate in its actual waiting state, without owner proof.

    Raises:
        ValueError: If policy, evidence, scope, targets or transaction fail.
    """
    require_transaction(conn, now)
    value = CandidateInput.model_validate_json(canonical_json(value))
    if not value.result.changes:
        raise ValueError("rejected_validation")
    row = conn.execute(
        "SELECT * FROM review_request WHERE id=?", (value.request_id,)
    ).fetchone()
    if row is None or row["state"] in {"cancelled", "failed"}:
        raise ValueError("rejected_validation")
    require_current_tombstones(conn, home)
    scope = Scope.model_validate_json(row["scope_json"])
    result = _normalize(value.result)
    ids, targets = _references(result)
    evidence = _originals(conn, home, row, ids)
    validate_batch(
        conn,
        result,
        scope,
        evidence,
        defer_candidate_failure=False,
        allow_missing_owner=True,
    )
    disposition = _disposition(conn, value.disposition, scope)
    if disposition is not None:
        targets += tuple(r for r in disposition.affected if r not in targets)
    candidate = Candidate(
        id=f"D_{uuid4().hex}",
        result=result,
        scope=scope,
        original_evidence_ids=ids,
        targets=targets,
        normalization_version=1,
        enrichment_version=1,
        disposition=disposition,
        digest="0" * 64,
    )
    candidate = candidate.model_copy(
        update={"digest": candidate_digest(candidate.model_dump(mode="json"))}
    )
    conn.execute(
        "INSERT INTO memory_candidate(id,request_id,payload_json,digest,"
        "normalization_version,enrichment_version,origin_unresolved_id,state,"
        "revision,created_at) "
        "VALUES(?,?,?,?,1,1,?,'waiting_verification',0,?)",
        (
            candidate.id,
            value.request_id,
            canonical_json(candidate),
            candidate.digest,
            disposition.unresolved_id if disposition is not None else None,
            now,
        ),
    )
    conn.executemany(
        "INSERT INTO memory_candidate_support VALUES(?,?,'original')",
        [(candidate.id, ident) for ident in ids],
    )
    return candidate_view(conn, candidate.id)


def _candidate(row: sqlite3.Row) -> Candidate | None:
    """Reject malformed or mismatched durable fields before any display."""
    if row["payload_json"] is None:
        if row["state"] != "erased":
            raise ValueError("decision_invalid")
        return None
    try:
        candidate = Candidate.model_validate_json(row["payload_json"])
        if (
            candidate.id != row["id"]
            or candidate.digest != row["digest"]
            or candidate_digest(candidate.model_dump(mode="json"))
            != candidate.digest
            or candidate.normalization_version != row["normalization_version"]
            or candidate.enrichment_version != row["enrichment_version"]
            or (
                candidate.disposition.unresolved_id
                if candidate.disposition
                else None
            )
            != row["origin_unresolved_id"]
        ):
            raise ValueError("decision_invalid")
        return candidate
    except ValueError:
        raise ValueError("decision_invalid") from None


def candidate_view(
    conn: sqlite3.Connection, ident: DecisionId
) -> DecisionView:
    """Read actual state without changing proof, admission or lifecycle."""
    try:
        TypeAdapter(DecisionId).validate_python(ident)
        row = conn.execute(
            "SELECT * FROM memory_candidate WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            raise ValueError("decision_unknown")
        candidate = _candidate(row)
        receipt = (
            stored_receipt(conn, row["application_request_id"])
            if row["application_request_id"]
            else None
        )
        intent = conn.execute(
            "SELECT state FROM memory_decision WHERE candidate_id=? "
            "ORDER BY submitted_at DESC,id DESC LIMIT 1",
            (ident,),
        ).fetchone()
        return DecisionView(
            id=ident,
            digest=row["digest"],
            state=row["state"],
            revision=row["revision"],
            candidate=candidate,
            receipt=receipt,
            code=row["code"],
            intent_state=(
                intent[0] if intent and intent[0] != "erased" else None
            ),
        )
    except ValueError as exc:
        if str(exc) == "decision_unknown":
            raise
        raise ValueError("decision_invalid") from None


def show_candidate(
    conn: sqlite3.Connection,
    ident: DecisionId,
    max_bytes: int,
) -> DecisionDisplay:
    """Return a complete verified display or refuse without partial output.

    Args:
        conn: Caller-owned readable connection.
        ident: Exact candidate identity.
        max_bytes: Positive UTF-8 byte ceiling for the entire displayed text.

    Returns:
        Complete candidate fields and the frozen support enrichment rule.

    Raises:
        ValueError: If the candidate is missing, erased, corrupt or too large.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("decision_display_limit")
    view = candidate_view(conn, ident)
    if view.candidate is None:
        raise ValueError("decision_erased")
    text = canonical_json_value(
        {
            "candidate": view.candidate.model_dump(mode="json"),
            "enrichment": {"version": 1, "rule": ENRICHMENT_RULE},
        }
    )
    if len(text.encode()) > max_bytes:
        raise ValueError("decision_display_limit")
    return DecisionDisplay(
        id=ident, digest=view.digest, text=text, complete=True
    )
