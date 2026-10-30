"""Share immutable commit context, admission checks and durable replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from muninn.knowledge_models import (
    AttemptId,
    Digest,
    EvidenceId,
    RequestId,
    StrictModel,
    UtcSeconds,
)
from muninn.knowledge_write import json_value
from muninn.review_candidates import CommitRejectedError
from muninn.review_evidence_models import PositiveInt
from muninn.review_json import canonical_json
from muninn.review_models import Receipt, ReviewPolicy
from muninn.review_progress import checked_policy, digest


class CommitContext(StrictModel):
    """Carry only program-owned identities into the writer boundary."""

    home: Path
    request_id: RequestId
    attempt_id: AttemptId
    generation: PositiveInt
    evidence_ids: tuple[EvidenceId, ...]
    now: UtcSeconds
    knowledge_digest: Digest | None = None


EVIDENCE_ERRORS = {
    "evidence_erased": "rejected_erased",
    "evidence_changed": "rejected_evidence",
    "evidence_unavailable": "rejected_evidence",
    "evidence_limit": "evidence_limit",
    "evidence_policy": "rejected_validation",
    "source_unavailable": "rejected_evidence",
    "incomplete_window": "rejected_evidence",
    "rejected_scope": "rejected_scope",
    "original_unavailable": "rejected_evidence",
    "ancestry_unavailable": "rejected_evidence",
    "scope_unavailable": "rejected_scope",
    "forbidden_selector": "rejected_evidence",
    "invalid_span": "rejected_evidence",
    "redacted_span": "rejected_evidence",
    "empty_evidence": "rejected_evidence",
}


def stored_receipt(
    conn: sqlite3.Connection, request_id: RequestId
) -> Receipt | None:
    """Reload the one durable success before consulting mutable state."""
    row = conn.execute(
        "SELECT * FROM review_receipt WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return Receipt.model_validate_json(
        json_value(
            {
                "schema_version": 1,
                "request_id": row["request_id"],
                "attempt_id": row["attempt_id"],
                "applied": json.loads(row["applied_json"]),
                "unresolved_ids": json.loads(row["unresolved_json"]),
                "committed_at": row["committed_at"],
                "outcome": row["outcome"],
                "no_change_reason": row["no_change_reason"],
            }
        )
    )


def checked_request_policy(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> ReviewPolicy:
    """Check immutable admission identities without calling admission reads."""
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(row["policy_json"])
        )
        control = conn.execute("SELECT * FROM review_control").fetchone()
        if (
            not control["admission"]
            or row["policy_digest"] != digest(policy)
            or control["policy_digest"] != digest(policy)
            or control["policy_json"] is None
            or canonical_json(
                ReviewPolicy.model_validate_json(control["policy_json"])
            )
            != canonical_json(policy)
        ):
            raise CommitRejectedError("rejected_validation")
    except ValueError:
        raise CommitRejectedError("rejected_validation") from None
    return policy
