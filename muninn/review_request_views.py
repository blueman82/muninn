"""Project durable review request and attempt rows into public views."""

from __future__ import annotations

import json
import sqlite3

from muninn.knowledge_models import AttemptId, RequestId, Scope
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_models import (
    AttemptView,
    CallView,
    CorrectionStatus,
    RequestView,
)


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
    del data["requested_json"]
    for key in ("observed", "usage"):
        value = data.pop(f"{key}_json")
        data[key] = json.loads(value) if value is not None else None
    return AttemptView.model_validate_json(
        json.dumps({"schema_version": 1, **data})
    )


def call_view(row: sqlite3.Row) -> CallView:
    """Decode a durable call without filling unreported observations."""
    data = {
        key: row[key]
        for key in CallView.model_fields
        if key not in ("observed", "usage", "process_identity")
    }
    for key in ("observed", "usage", "process_identity"):
        value = row[f"{key}_json"]
        data[key] = json.loads(value) if value is not None else None
    return CallView.model_validate_json(json.dumps(data))


def correction_status(
    conn: sqlite3.Connection, request_id: RequestId
) -> CorrectionStatus:
    """Use the latest marked attempt and only its exact launched successor."""
    marker = conn.execute(
        "SELECT a.id,a.ended_at,a.error_code,m.round_index,m.state,"
        "m.launched_at,m.ended_at FROM review_attempt a JOIN review_call m "
        "ON m.attempt_id=a.id WHERE a.request_id=? AND m.outcome IN "
        "('correction_format_requested','correction_reduction_requested') "
        "ORDER BY a.started_at DESC,a.id DESC,m.round_index DESC LIMIT 1",
        (request_id,),
    ).fetchone()
    if (
        marker is None
        or marker[4] != "completed"
        or marker[5] is None
        or marker[6] is None
    ):
        return "unknown"
    successor = conn.execute(
        "SELECT state,launched_at,ended_at FROM review_call "
        "WHERE attempt_id=? AND round_index=?",
        (marker[0], marker[3] + 1),
    ).fetchone()
    if successor is None or successor[1] is None:
        return "requested"
    if successor[0] != "completed" or successor[2] is None:
        return "attempted"
    receipt = conn.execute(
        "SELECT 1 FROM review_receipt WHERE request_id=? AND attempt_id=?",
        (request_id, marker[0]),
    ).fetchone()
    if receipt is not None:
        return "succeeded"
    if marker[1] is not None and marker[2] is not None:
        return "exhausted"
    return "attempted"
