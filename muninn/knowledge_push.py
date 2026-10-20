"""Build bounded eligible note context and a compact current guide index."""

from __future__ import annotations

import sqlite3

from muninn.knowledge_models import (
    GuideBody,
    GuideIndex,
    KnowledgeContext,
    NoteBody,
    Scope,
)
from muninn.knowledge_read import (
    candidate_rows,
    eligible_current,
    read_snapshot,
    unresolved_ids,
    validate_time,
)
from muninn.review_json import canonical_json

# Preserve bounded candidate validation while allowing configured byte limits.
_SCAN_CAP = 200


def _size(context: KnowledgeContext) -> int:
    """Count the complete canonical UTF-8 envelope, including metadata."""
    return len(canonical_json(context).encode("utf-8"))


def knowledge_context(
    conn: sqlite3.Connection, scope: Scope, max_bytes: int, now: float
) -> KnowledgeContext:
    """Return current supported notes and guide labels within a byte cap.

    Args:
        conn: Open store connection; existing transactions are preserved.
        scope: Explicit mapped project/task or authorized global scope.
        max_bytes: Positive cap on the entire canonical UTF-8 envelope.
        now: Explicit finite nonnegative expiry evaluation time.

    Returns:
        Complete eligible notes and compact guide IDs/revisions. Oversized
        items are omitted whole and set truncated; bodies are never sliced.

    Raises:
        ValueError: If time is invalid or the cap cannot fit the envelope.
    """
    validate_time(now)
    context = KnowledgeContext(
        schema_version=1,
        notes=[],
        guides=[],
        unresolved_ids=[],
        truncated=False,
    )
    if type(max_bytes) is not int or max_bytes < _size(context):
        raise ValueError("context_limit")
    omitted = False
    with read_snapshot(conn):
        for row in candidate_rows(conn, scope, limit=_SCAN_CAP):
            omitted = omitted or row["candidate_total"] > _SCAN_CAP
            try:
                view, _ = eligible_current(conn, row, scope, now)
            except ValueError:
                continue
            notes, guides = list(context.notes), list(context.guides)
            if isinstance(view.body, NoteBody):
                notes.append(view)
            elif isinstance(view.body, GuideBody):
                guides.append(
                    GuideIndex(
                        id=view.id,
                        revision=view.revision,
                        title=view.body.title,
                        scope=view.body.scope,
                    )
                )
            candidate = context.model_copy(
                update={"notes": notes, "guides": guides}
            )
            if _size(candidate) <= max_bytes:
                context = candidate
            else:
                omitted = True
        for ident in unresolved_ids(conn, scope):
            candidate = context.model_copy(
                update={"unresolved_ids": [*context.unresolved_ids, ident]}
            )
            if _size(candidate) <= max_bytes:
                context = candidate
            else:
                omitted = True
    return context.model_copy(update={"truncated": omitted})
