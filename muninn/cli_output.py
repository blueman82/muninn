"""Output shaping: redact every text field just before it is printed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from muninn import classify

__all__ = ["redacted"]

# Fields known to carry transcript or user text; only these are redacted.
_TEXT_KEYS = frozenset(
    {"text", "snippet", "preview", "quote", "first_prompt", "retract_reason"}
)


def _redact_snippet(value: str) -> str:
    """Redact a search snippet that may contain match marks."""
    # A «match» mark can split a secret in two, hiding it from the
    # redactor; test the text without marks and drop the marks (and the
    # highlight) only when something was found.
    plain = value.replace("«", "").replace("»", "")
    clean, changed = classify.redact(plain)
    return clean if changed else value


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Redact the values of ``value``, withholding a raw line that changes."""
    out: dict[str, Any] = {}
    for k, v in value.items():
        # A byte-exact "raw" line cannot be partly masked without lying
        # about what the file holds, so it is withheld whole.
        if k == "raw" and isinstance(v, str) and classify.redact(v)[1]:
            out["raw_redacted"] = True
        else:
            out[k] = redacted(v, k)
    return out


def redacted(value: Any, key: str | None = None) -> Any:
    """Return ``value`` with every text field passed through the redactor.

    Redaction already happens at ingest; doing it again here is defence in
    depth, so a line stored before a rule existed never reaches the screen.

    Args:
        value: A JSON-shaped value (dict, list, string or scalar).
        key: Name of the field holding ``value``; only text fields change.

    Returns:
        A copy of the same shape, except that a ``raw`` string the
        redactor would change is replaced by a ``raw_redacted: True`` key.
    """
    # The value is parsed JSON, so dict keys are always strings and list
    # items are JSON values; isinstance alone leaves their types unknown.
    if isinstance(value, dict):
        return _redact_mapping(cast("dict[str, Any]", value))
    if isinstance(value, list):
        return [redacted(v, key) for v in cast("list[Any]", value)]
    if isinstance(value, str) and key in _TEXT_KEYS:
        if key == "snippet":
            return _redact_snippet(value)
        return classify.redact(value)[0]
    return value
