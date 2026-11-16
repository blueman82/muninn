"""Small helpers that shape one answer: error dicts, refs, previews."""

from __future__ import annotations

import sqlite3
from typing import Any

from muninn.knowledge_models import GuideBody, KnowledgeView, NoteBody, Scope
from muninn.knowledge_proof import too_long
from muninn.query.constants import NOTICE

# Answers are plain JSON-serialisable dicts whose shape varies per query.
type Answer = dict[str, Any]

PREVIEW = 200  # chars of one event preview


class BadArgumentError(Exception):
    """Input the query functions refuse; str() is the error code."""


def error(code: str) -> Answer:
    """Return the standard refusal answer for an error code."""
    return {"error": code, "notice": NOTICE}


def event_ref(row: sqlite3.Row) -> str:
    """Return the provider:thread_id:line.part REF of an event row."""
    return f"{row['provider']}:{row['thread_id']}:{row['line']}.{row['part']}"


def answer_citable(row: sqlite3.Row) -> bool:
    """Say whether the event may support an answer (not ledger validity)."""
    return bool(
        row["thread_class"] == "primary"
        and row["kind"] in ("prompt", "reply")
        and not row["flags"] & 1
    )


def preview(text: str) -> str:
    """Collapse whitespace and cut the text to one preview line."""
    return " ".join(text.split())[:PREVIEW]


def knowledge_scopes(
    conn: sqlite3.Connection,
    ids: list[int],
    everywhere: bool,
    task: str | None = None,
) -> list[Scope]:
    """Resolve mapped scopes for authoritative current knowledge readers."""
    if not everywhere and not ids:
        return []
    predicate = (
        ""
        if everywhere
        else "WHERE kind!='global' AND " f"id IN ({','.join('?' * len(ids))})"
    )
    rows = conn.execute(
        f"SELECT key,kind FROM scope {predicate} ORDER BY id",
        () if everywhere else ids,
    )
    return [
        Scope(
            project_id=None if row["kind"] == "global" else row["key"],
            task=None if row["kind"] == "global" else task,
        )
        for row in rows
    ]


def knowledge_preview(view: KnowledgeView, *, proof: bool = False) -> Answer:
    """Render a revision-aware preview with governed support identities.

    Args:
        view: The view as agents see it.
        proof: The text is an owner proof, which is never cut: a long one
            becomes a look-up line, and the preview is marked ``proof``.

    Returns:
        The preview answer.

    Raises:
        ValueError: If the view has no current body.
    """
    body = view.body
    if isinstance(body, NoteBody):
        text, refs = body.content, body.evidence_refs
    elif isinstance(body, GuideBody):
        text, refs = body.title, body.summary.evidence_refs
    else:
        raise ValueError("knowledge_ineligible")
    if proof and len(text) > 300:
        text = too_long(view.id)
    preview: Answer = {
        "id": view.id,
        "revision": view.revision,
        "state": view.state,
        "form": body.form,
        "kind": body.kind,
        "text": text[:300],
        "scope": body.scope.model_dump(mode="json"),
        "evidence_refs": refs,
    }
    return preview | {"proof": True} if proof else preview
