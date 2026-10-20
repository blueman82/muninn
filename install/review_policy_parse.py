"""Reject ambiguous policy JSON before validating its typed contract."""

from __future__ import annotations

import json

from install.context import StepFailedError
from muninn.review_json import reject_constant
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse duplicate keys that ordinary JSON loading would discard."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_policy_key")
        result[key] = value
    return result


def policy(raw: str) -> ReviewPolicy:
    """Validate one strict recorded policy without guessing defaults."""
    try:
        json.loads(
            raw, object_pairs_hook=_object, parse_constant=reject_constant
        )
        return checked_policy(ReviewPolicy.model_validate_json(raw))
    except (ValueError, RecursionError, TypeError):
        raise StepFailedError("review_policy_invalid") from None
