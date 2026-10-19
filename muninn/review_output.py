"""Decode bounded native output using data-only review contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from pydantic import ValidationError

from muninn.review_json import finite_float, reject_constant, unique_object
from muninn.review_models import ObservedConfig, Usage


def output_contract_digest() -> str:
    """Bind proof issuance to the exact decoder and metadata interpretation."""
    descriptor = {
        "contract": "muninn-claude-output-v1",
        "json": "one-utf8-object;no-duplicates-trailing-nonfinite",
        "success": {"type": "result", "subtype": "success", "is_error": False},
        "structured_output": (
            "object;canonical-sorted-compact-utf8;" "main-validates-round"
        ),
        "permission_denials": "absent-null-empty-list-only",
        "deferred_tool_use": "absent-null-only",
        "modelUsage": (
            "absent-null-or-map-of-nonempty-model-to-object;"
            "singleton-reports-model"
        ),
        "observed": {"runner": None, "version": None, "reasoning": None},
        "usage": (
            "absent-null-or-object;"
            "present-counters-null-or-nonnegative-integer"
        ),
        "tokens": {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cached_tokens",
        },
        "cost": "all-estimates-ignored;cost_decimal-and-currency-null",
        "bounds": "positive-explicit-integer;whole-stdout-and-canonical-body",
        "extras": "bounded-envelope-fields-ignored;no-authority",
    }
    return hashlib.sha256(
        json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def reported_metadata(
    observed: Mapping[str, object], usage: Mapping[str, object] | None
) -> tuple[ObservedConfig, Usage | None]:
    """Validate explicit observations and preserve unknown measures."""
    try:
        config = ObservedConfig.model_validate(
            {**dict.fromkeys(ObservedConfig.model_fields), **observed}
        )
        measured = (
            Usage.model_validate(
                {**dict.fromkeys(Usage.model_fields), **usage}
            )
            if usage is not None
            else None
        )
        return config, measured
    except ValidationError:
        raise ValueError("invalid_reported_metadata") from None


def _object(value: object) -> dict[str, object]:
    """Require a parsed JSON object without coercing other shapes."""
    if not isinstance(value, dict):
        raise ValueError("invalid_native_output")
    return cast(dict[str, object], value)


def _metadata(
    envelope: dict[str, object],
) -> tuple[ObservedConfig, Usage | None]:
    """Map explicit native counters without promoting estimates to charges."""
    models = envelope.get("modelUsage")
    model = None
    if models is not None:
        entries = _object(models)
        if any(
            not key.strip() or not isinstance(value, dict)
            for key, value in entries.items()
        ):
            raise ValueError("invalid_native_output")
        if len(entries) == 1:
            model = next(iter(entries))
    usage = envelope.get("usage")
    measured: dict[str, object] | None = None
    if usage is not None:
        counters = _object(usage)
        measured = {}
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cached_tokens"),
        ):
            value = counters.get(source)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid_native_output")
            measured[target] = value
    return reported_metadata({"model": model}, measured)


def decode_claude_output(
    raw: bytes, max_output_bytes: int
) -> tuple[bytes, ObservedConfig, Usage | None]:
    """Decode only a bounded native success object without executing it."""
    if (
        type(raw) is not bytes
        or type(max_output_bytes) is not int
        or max_output_bytes <= 0
        or len(raw) > max_output_bytes
    ):
        raise ValueError("native_output_limit")
    try:
        envelope = _object(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
                parse_float=finite_float,
            )
        )
        if (
            envelope.get("type") != "result"
            or envelope.get("subtype") != "success"
            or envelope.get("is_error") is not False
            or envelope.get("permission_denials") not in (None, [])
            or envelope.get("deferred_tool_use") is not None
        ):
            raise ValueError("invalid_native_output")
        body = json.dumps(
            _object(envelope.get("structured_output")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        observed, usage = _metadata(envelope)
    except (ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid_native_output") from None
    if len(body) > max_output_bytes:
        raise ValueError("native_output_limit")
    return body, observed, usage
