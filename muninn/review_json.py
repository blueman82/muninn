"""Validate external review JSON and encode canonical model projections."""

from __future__ import annotations

import json
import math

from muninn.knowledge_models import ReviewResult, StrictModel


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys, including escaped equivalent names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value: str) -> object:
    """Reject JSON extensions for nonfinite numeric constants."""
    raise ValueError("nonfinite_json_number")


def finite_float(value: str) -> float:
    """Reject numeric literals that overflow the finite float range."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite_json_number")
    return result


def parse_result(raw: bytes, limit: int) -> ReviewResult:
    """Validate bounded UTF-8 JSON before strict result parsing.

    Args:
        raw: Untrusted model response bytes.
        limit: Explicit maximum response size in bytes.

    Returns:
        A validated result without allocated lifecycle values.

    Raises:
        ValueError: If encoding, JSON syntax, size or structure is invalid.
    """
    if type(limit) is not int or limit <= 0 or len(raw) > limit:
        raise ValueError("invalid_result_size")
    json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )
    return ReviewResult.model_validate_json(raw, strict=True)


def canonical_json(value: StrictModel) -> str:
    """Encode a model using stable UTF-8-compatible canonical JSON."""
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def result_schema() -> dict[str, object]:
    """Generate the output schema from the same result definitions."""
    return ReviewResult.model_json_schema()
