"""Persist normalized knowledge revisions and explicit operator withdrawals."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from muninn.knowledge_models import (
    AffectedRevision,
    Body,
    Change,
    EvidenceId,
    GuideBody,
    KnowledgeId,
    KnowledgeView,
    NoteBody,
    Scope,
    UnresolvedId,
    UnresolvedProposal,
    nonempty_text,
)
from muninn.redaction import redact, redact_parts
from muninn.review_json import canonical_json
from muninn.review_progress import require_transaction
from muninn.review_requests import cancel_request


def json_value(value: object) -> str:
    """Encode ordinary persisted collections with the canonical JSON rules."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def body_of(change: Change) -> NoteBody | GuideBody:
    """Remove proposal controls before storing canonical version content."""
    data = change.model_dump(
        exclude={"action", "knowledge_id", "expected_revision"}
    )
    model = NoteBody if change.form == "note" else GuideBody
    return model.model_validate(data)


def redacted_body(body: Body) -> Body:
    """Sanitize only governed text after admission, preserving references."""
    data = body.model_dump()
    data["reason"] = redact(body.reason)[0]
    if isinstance(body, NoteBody):
        data["content"], *data["conditions"] = redact_parts(
            [body.content, *body.conditions]
        )
    else:
        data["title"] = redact(body.title)[0]
        supported = [
            data["summary"],
            *data["applicability"],
            *data["decision_points"],
            *data["pitfalls"],
            *(step["claim"] for step in data["steps"]),
        ]
        # This order must match claims so cross-field secrets seen by FTS
        # are also removed from canonical body text and normalized claims.
        for claim, text in zip(
            supported,
            redact_parts([claim["text"] for claim in supported]),
            strict=True,
        ):
            claim["text"] = text
    return type(body).model_validate(data)


def claims(body: Body) -> list[tuple[str, str, list[EvidenceId], list[str]]]:
    """Normalize supported assertions and ordered same-version dependencies."""
    if isinstance(body, NoteBody):
        return [
            (
                "note",
                "\n".join([body.content, *body.conditions]),
                body.evidence_refs,
                [],
            )
        ]
    rows: list[tuple[str, str, list[EvidenceId], list[str]]] = [
        ("summary", body.summary.text, body.summary.evidence_refs, [])
    ]
    for prefix, items in (
        ("applicability", body.applicability),
        ("decision", body.decision_points),
        ("pitfall", body.pitfalls),
    ):
        rows.extend(
            (f"{prefix}/{i}", item.text, item.evidence_refs, [])
            for i, item in enumerate(items)
        )
    rows.extend(
        (
            f"step/{step.key}",
            step.claim.text,
            step.claim.evidence_refs,
            [f"step/{key}" for key in step.depends_on],
        )
        for step in body.steps
    )
    return rows


def target(
    conn: sqlite3.Connection, affected: AffectedRevision, scope: Scope
) -> sqlite3.Row:
    """Require a retained current revision in exactly the requested scope."""
    row = conn.execute(
        "SELECT k.*,v.kind,v.scope_json,v.content_json,v.erased_at "
        "FROM knowledge k JOIN knowledge_version v ON v.knowledge_id=k.id "
        "AND v.revision=k.current_revision WHERE k.id=?",
        (affected.knowledge_id,),
    ).fetchone()
    if (
        row is None
        or row["current_revision"] != affected.revision
        or row["state"] not in {"active", "withheld"}
        or row["content_json"] is None
        or row["erased_at"] is not None
    ):
        raise ValueError("rejected_revision")
    if Scope.model_validate_json(row["scope_json"]) != scope:
        raise ValueError("rejected_scope")
    return row


def apply_change(
    conn: sqlite3.Connection, change: Change, request: sqlite3.Row, now: float
) -> AffectedRevision:
    """Write an already checked body, its support, and its active FTS row."""
    body = redacted_body(body_of(change))
    if change.action == "add":
        ident, revision = f"K_{uuid4().hex}", 1
        conn.execute(
            "INSERT INTO knowledge VALUES(?,?,'active',NULL,NULL)",
            (ident, revision),
        )
    else:
        ident, revision = change.knowledge_id, change.expected_revision + 1
        conn.execute(
            "UPDATE knowledge SET current_revision=?,state='active' "
            "WHERE id=?",
            (revision, ident),
        )
    conn.execute(
        "INSERT INTO knowledge_version VALUES(?,?,?,?,?,?,?,?,?,NULL,?,NULL)",
        (
            ident,
            revision,
            request["id"],
            body.form,
            body.kind,
            canonical_json(body.scope),
            canonical_json(body),
            body.reason,
            request["origin"],
            now,
        ),
    )
    normalized = claims(body)
    for key, text, evidence, dependencies in normalized:
        conn.execute(
            "INSERT INTO knowledge_claim VALUES(?,?,?,?,?,'live')",
            (ident, revision, key, text, json_value(dependencies)),
        )
        conn.executemany(
            "INSERT INTO knowledge_support VALUES(?,?,?,?)",
            [(ident, revision, key, item) for item in evidence],
        )
    conn.execute("DELETE FROM knowledge_fts WHERE knowledge_id=?", (ident,))
    conn.execute(
        "INSERT INTO knowledge_fts VALUES(?,?,?)",
        (ident, revision, "\n".join(row[1] for row in normalized)),
    )
    return AffectedRevision(knowledge_id=ident, revision=revision)


def save_unresolved(
    conn: sqlite3.Connection,
    request_id: str,
    proposals: list[UnresolvedProposal],
    now: float,
) -> list[UnresolvedId]:
    """Persist questions and withdraw every affected revision and step."""
    output: list[UnresolvedId] = []
    for proposal in proposals:
        proposal = UnresolvedProposal.model_validate(
            {
                **proposal.model_dump(),
                "claim": redact(proposal.claim)[0],
                "question": redact(proposal.question)[0],
            }
        )
        ident = f"U_{uuid4().hex}"
        conn.execute(
            "INSERT INTO unresolved VALUES(?,?,?,?,?,?,'open')",
            (
                ident,
                request_id,
                canonical_json(proposal),
                json_value([item.model_dump() for item in proposal.affected]),
                json_value(proposal.evidence_refs),
                now,
            ),
        )
        for affected in proposal.affected:
            conn.execute(
                "UPDATE knowledge SET state='withheld' WHERE id=? "
                "AND current_revision=?",
                (affected.knowledge_id, affected.revision),
            )
            conn.execute(
                "UPDATE knowledge_claim SET state='withheld' "
                "WHERE knowledge_id=? AND revision=?",
                (affected.knowledge_id, affected.revision),
            )
            conn.execute(
                "DELETE FROM knowledge_fts WHERE knowledge_id=? "
                "AND revision=?",
                (affected.knowledge_id, affected.revision),
            )
        output.append(ident)
    return output


def knowledge_view(
    conn: sqlite3.Connection, ident: KnowledgeId
) -> KnowledgeView:
    """Load retained version content and program-owned lifecycle fields."""
    row = conn.execute(
        "SELECT k.*,v.content_json,v.origin,v.expiry_json,v.created_at "
        "FROM knowledge k JOIN knowledge_version v ON v.knowledge_id=k.id "
        "AND v.revision=k.current_revision WHERE k.id=?",
        (ident,),
    ).fetchone()
    if row is None:
        raise ValueError("rejected_revision")
    return KnowledgeView.model_validate_json(
        json_value(
            {
                "schema_version": 1,
                "id": ident,
                "revision": row["current_revision"],
                "state": row["state"],
                "body": (
                    json.loads(row["content_json"])
                    if row["content_json"] is not None
                    else None
                ),
                "origin": row["origin"],
                "expiry": (
                    json.loads(row["expiry_json"])
                    if row["expiry_json"] is not None
                    else None
                ),
                "created_at": row["created_at"],
                "retracted_at": row["retracted_at"],
                "retracted_reason": row["retracted_reason"],
            }
        )
    )


def retract_knowledge(
    conn: sqlite3.Connection,
    knowledge_id: KnowledgeId,
    expected_revision: int,
    reason: str,
    now: float,
) -> KnowledgeView:
    """Atomically withdraw a current item and fence known associated work.

    Args:
        conn: Connection under the caller's existing writer lock.
        knowledge_id: Explicit operator-selected retained item.
        expected_revision: Current revision required by the operator.
        reason: Governed nonempty operator explanation.
        now: Finite nonnegative timestamp.

    Returns:
        The retained version with its retraction metadata.

    Raises:
        ValueError: If input, current revision, or transaction ownership fails.
    """
    if conn.in_transaction:
        raise ValueError("transaction_already_active")
    affected = AffectedRevision(
        knowledge_id=knowledge_id, revision=expected_revision
    )
    reason = nonempty_text(reason)
    conn.execute("BEGIN IMMEDIATE")
    try:
        require_transaction(conn, now)
        view = knowledge_view(conn, knowledge_id)
        if view.body is None:
            raise ValueError("rejected_revision")
        target(conn, affected, view.body.scope)
        reason = nonempty_text(redact(reason)[0])
        conn.execute(
            "UPDATE knowledge SET state='retracted',retracted_at=?,"
            "retracted_reason=? WHERE id=?",
            (now, reason, knowledge_id),
        )
        conn.execute(
            "DELETE FROM knowledge_fts WHERE knowledge_id=?", (knowledge_id,)
        )
        requests = conn.execute(
            "SELECT DISTINCT r.id FROM review_request r JOIN review_input i "
            "ON i.request_id=r.id JOIN knowledge_support s "
            "ON s.evidence_id=i.evidence_id "
            "WHERE s.knowledge_id=? AND s.revision=? "
            "AND r.state IN ('pending','running','waiting')",
            (knowledge_id, expected_revision),
        ).fetchall()
        for row in requests:
            cancel_request(conn, row["id"], now)
        result = knowledge_view(conn, knowledge_id)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
