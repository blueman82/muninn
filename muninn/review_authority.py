"""Recognize owner speech by message provenance, never by its wording."""

from __future__ import annotations

import re
from typing import Any

from muninn import classify
from muninn.claude_events import (
    CHANNEL_HUMAN_FLAG,
    CLAUDE_HARNESS_PREFIXES,
)
from muninn.codex_events import CHATGPT_REFERENCE_TAG, HARNESS_TAGS
from muninn.event_model import FLAG_MARKERS, as_record

# A whole message wrapped in one tag is program-made, whatever the name.
_WRAPPED = re.compile(r"\A<([A-Za-z_][\w:.-]*)\b[^>]*>.*</\1>\Z", re.S)
_GENERATED = (
    *HARNESS_TAGS,
    *CLAUDE_HARNESS_PREFIXES,
    CHATGPT_REFERENCE_TAG,
    *FLAG_MARKERS,
)


def injected(record: dict[str, Any]) -> bool:
    """Reject provider annotations that identify generated or relayed input."""
    flags = (
        "isMeta",
        "isSidechain",
        "isCompactSummary",
        CHANNEL_HUMAN_FLAG,
    )
    containers = [record]
    for key in ("payload", "message"):
        nested = as_record(record.get(key))
        if nested is not None:
            containers.append(nested)
    return any(
        any(item.get(flag) is True for flag in flags)
        or "toolUseResult" in item
        for item in containers
    )


def direct_speech(
    provider: str,
    context: str,
    record: dict[str, Any],
) -> bool:
    """Require owner provenance for the whole physical message.

    Args:
        provider: ``codex``, ``claude`` or ``cursor``.
        context: All owner-statement fields from the physical message.
        record: Provider record containing any origin or injection flags.

    Returns:
        Whether the provider labelled the message as typed by the owner,
        with no injection flag, harness tag or Muninn marker anywhere in it,
        and the message is not one tagged element.
    """
    return (
        classify.user_text_kind(provider, record) == "prompt"
        and not injected(record)
        and not any(marker in context for marker in _GENERATED)
        and _WRAPPED.match(context.strip()) is None
    )
