"""Project durable review request and attempt rows into public views."""

from __future__ import annotations

import json
import sqlite3

from muninn.knowledge_models import AttemptId, RequestId, Scope
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_models import AttemptView, RequestView


def request_row(
    conn: sqlite3.Connection, request_id: RequestId
) -> sqlite3.Row:
    """Load one request without manufacturing a missing identity."""
    row = conn.execute(
        "SELECT * FROM review_request WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError("request_unavailable")
    return row


def request_view(row: sqlite3.Row, accepted: bool = True) -> RequestView:
    """Project durable request columns through the strict public model."""
    return RequestView(
        schema_version=1,
        id=row["id"],
        window=EvidenceWindow(
            source=SourceRef(
                provider=row["provider"], thread_id=row["thread_id"]
            ),
            from_line=row["from_line"],
            through_line=row["through_line"],
            boundary_hash=row["boundary_hash"],
        ),
        scope=Scope.model_validate_json(row["scope_json"]),
        policy_digest=row["policy_digest"],
        state=row["state"],
        current_attempt_id=row["current_attempt_id"],
        created_at=row["created_at"],
        retry_at=row["retry_at"],
        accepted=accepted,
    )


def attempt_view(
    conn: sqlite3.Connection, attempt_id: AttemptId
) -> AttemptView:
    """Load one attempt through the strict public model."""
    row = conn.execute(
        "SELECT * FROM review_attempt WHERE id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise ValueError("attempt_unavailable")
    data = dict(row)
    for key in ("requested", "observed", "usage"):
        value = data.pop(f"{key}_json")
        data[key] = json.loads(value) if value is not None else None
    return AttemptView.model_validate_json(
        json.dumps({"schema_version": 1, **data})
    )
