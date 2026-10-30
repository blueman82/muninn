"""Derive deterministic memory application identity without I/O."""

from __future__ import annotations

import hashlib

from muninn.decision_models import DecisionId
from muninn.review_json import canonical_json_value


def application_key(ident: DecisionId, frozen: str, revision: int) -> str:
    """Hash only the frozen candidate and accepted revision in a new domain."""
    payload = canonical_json_value(
        {
            "candidate_id": ident,
            "candidate_digest": frozen,
            "decision_revision": revision,
        }
    ).encode()
    result = hashlib.sha256()
    for part in (b"muninn-memory-application-v1", payload):
        result.update(len(part).to_bytes(8, "big"))
        result.update(part)
    return result.hexdigest()
