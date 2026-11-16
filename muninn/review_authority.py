"""Recognize owner speech by message provenance, never by its wording."""

from __future__ import annotations

from typing import Any

from muninn.claude_events import CHANNEL_HUMAN_FLAG
from muninn.codex_events import CHATGPT_REFERENCE_TAG
from muninn.event_model import FLAG_MARKERS, as_list, as_record

_MARKERS = (CHATGPT_REFERENCE_TAG, *FLAG_MARKERS)


def labelled_owner(provider: str, record: dict[str, Any]) -> bool:
    """Read the provider's own label that a person typed the message.

    Args:
        provider: ``codex``, ``claude`` or ``cursor``.
        record: Provider record of one user message.

    Returns:
        For Claude, ``origin.kind`` is ``human``; a record without an
        origin gets no trust. For Codex, every content item's metadata
        has provenance type ``user``. Cursor user bubbles are trusted.
    """
    if provider == "cursor":
        return True
    if provider == "claude":
        origin = as_record(record.get("origin"))
        return origin is not None and origin.get("kind") == "human"
    payload = as_record(record.get("payload")) or {}
    passthrough = (
        as_record(payload.get("internal_chat_message_metadata_passthrough"))
        or {}
    )
    items = as_list(passthrough.get("content_item_metadata")) or []
    content = as_list(payload.get("content")) or []
    labels: list[object] = []
    for item in items:
        meta = as_record(item) or {}
        labels.append((as_record(meta.get("provenance")) or {}).get("type"))
    return (
        bool(content)
        and len(labels) == len(content)
        and all(label == "user" for label in labels)
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
        Whether the provider's built-in label says a person typed the
        message, with no injection flag and no Muninn or ChatGPT marker in
        it. The wording is never read for trust.
    """
    return (
        labelled_owner(provider, record)
        and not injected(record)
        and not any(marker in context for marker in _MARKERS)
    )
