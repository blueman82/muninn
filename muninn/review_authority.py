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

SENTENCE = re.compile(r".+?(?:[.!;](?=\s|$)|$)", re.S)
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


def whole_sentences(text: str, span: tuple[int, int]) -> bool:
    """Require a selection to start and end on sentence boundaries.

    Args:
        text: The entire selected field before taking the span.
        span: Original Unicode start and end offsets of the selection.

    Returns:
        Whether the span covers only complete sentences, so a slice cannot
        drop a negation, condition or restriction at either end. A final
        terminator and whitespace may stay outside the span.
    """
    start, end = span
    # Newlines can continue an operand; they do not end a sentence.
    sentences = [
        sentence
        for sentence in SENTENCE.finditer(text)
        if sentence.start() < end
        and sentence.end() > start
        and sentence[0].strip(" .!;\t\r\n")
    ]
    return bool(sentences) and all(
        not text[sentence.start() : start].strip()
        and not text[end : sentence.end()].strip(" .!;\t\r\n")
        for sentence in sentences
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
        with no injection flag, harness tag or Muninn marker anywhere in it.
    """
    return (
        classify.user_text_kind(provider, record) == "prompt"
        and not injected(record)
        and not any(marker in context for marker in _GENERATED)
    )
