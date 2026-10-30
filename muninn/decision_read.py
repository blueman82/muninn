"""Read historical unresolved questions without changing governed state."""

from __future__ import annotations

import json
import sqlite3

from pydantic import TypeAdapter

from muninn.decision_models import UnresolvedView
from muninn.knowledge_models import AffectedRevision, Scope, UnresolvedId
from muninn.knowledge_scope import scope_applies


def show_unresolved(
    conn: sqlite3.Connection, ident: UnresolvedId
) -> UnresolvedView:
    """Read one historical question, including resolved and erased metadata.

    Args:
        conn: Caller-owned readable connection.
        ident: Exact historical question identity.

    Returns:
        Retained content and affected revision relevance, without admission.

    Raises:
        ValueError: If the ID is unknown or retained data is malformed.
    """
    try:
        ident = TypeAdapter(UnresolvedId).validate_python(ident)
    except ValueError:
        raise ValueError("unresolved_invalid") from None
    row = conn.execute(
        "SELECT u.*,r.scope_json FROM unresolved u JOIN review_request r "
        "ON r.id=u.request_id WHERE u.id=?",
        (ident,),
    ).fetchone()
    if row is None:
        raise ValueError("unresolved_unknown")
    try:
        erased = row["state"] == "erased"
        affected = TypeAdapter(tuple[AffectedRevision, ...]).validate_json(
            row["affected_json"]
        )
        stale = tuple(
            ref
            for ref in affected
            if not conn.execute(
                "SELECT 1 FROM knowledge k JOIN knowledge_version v "
                "ON v.knowledge_id=k.id AND v.revision=k.current_revision "
                "WHERE k.id=? AND k.current_revision=? "
                "AND k.state IN ('active','withheld') "
                "AND v.content_json IS NOT NULL AND v.erased_at IS NULL",
                (ref.knowledge_id, ref.revision),
            ).fetchone()
        )
        return UnresolvedView.model_validate_json(
            json.dumps(
                {
                    "id": row["id"],
                    "request_id": row["request_id"],
                    "state": row["state"],
                    "item": (
                        None
                        if erased or row["item_json"] is None
                        else json.loads(row["item_json"])
                    ),
                    "affected": [ref.model_dump() for ref in affected],
                    "stale_affected": [ref.model_dump() for ref in stale],
                    "support_ids": (
                        [] if erased else json.loads(row["support_json"])
                    ),
                    "scope": json.loads(row["scope_json"]),
                }
            )
        )
    except (ValueError, TypeError):
        raise ValueError("unresolved_invalid") from None


def list_unresolved(
    conn: sqlite3.Connection,
    scopes: tuple[Scope, ...],
    *,
    limit: int = 50,
    after: UnresolvedId | None = None,
) -> tuple[UnresolvedView, ...]:
    """List open questions in selected scopes using a stable ID cursor.

    Args:
        conn: Caller-owned readable connection.
        scopes: Explicit project, global or task scopes to inspect.
        limit: Maximum returned questions, from one through one hundred.
        after: Exclusive historical ID cursor.

    Returns:
        Applicable open questions in ascending identity order.

    Raises:
        ValueError: If a limit, cursor or retained question is invalid.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("unresolved_limit")
    try:
        if after is not None:
            TypeAdapter(UnresolvedId).validate_python(after)
        scopes = tuple(
            Scope.model_validate_json(s.model_dump_json()) for s in scopes
        )
    except ValueError:
        raise ValueError("unresolved_invalid") from None
    output: list[UnresolvedView] = []
    for row in conn.execute(
        "SELECT u.id,r.scope_json FROM unresolved u JOIN review_request r "
        "ON r.id=u.request_id WHERE u.state='open' AND u.id>? ORDER BY u.id",
        (after or "",),
    ):
        try:
            origin = Scope.model_validate_json(row["scope_json"])
        except ValueError:
            raise ValueError("unresolved_invalid") from None
        if any(scope_applies(origin, scope) for scope in scopes):
            output.append(show_unresolved(conn, row["id"]))
            if len(output) == limit:
                break
    return tuple(output)
