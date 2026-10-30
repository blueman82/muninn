"""Project observed failure phases and durable bounded correction history."""

from __future__ import annotations

import sqlite3
from typing import Literal

from muninn.knowledge_models import RequestId
from muninn.review_models import CorrectionStatus

type FailurePhase = Literal[
    "native output",
    "model format",
    "candidate validation",
    "controlled read",
    "original evidence",
    "commit",
]


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
