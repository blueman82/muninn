"""Recognize bounded direct speech after checking message provenance."""

from __future__ import annotations

import re
from typing import Any

from muninn.claude_events import (
    CHANNEL_HUMAN_FLAG,
    CLAUDE_HARNESS_PREFIXES,
)
from muninn.codex_events import CHATGPT_REFERENCE_TAG, HARNESS_TAGS
from muninn.event_model import as_record

_IDENTIFIER = re.compile(r"`[A-Za-z0-9_./:$-]+`")
_QUOTED = re.compile(r"[<>\"“”`]|(?:^|\s)'|'(?:$|\s|[.!?,])")
_IMPORTED = re.compile(
    r"\b(?:quot\w*|past(?:e|ed|ing)|cop(?:y|ied)|relay\w*|referenced|"
    r"transcript|excerpt|colleague)\b|"
    r"\b(?:assistant|tool|someone|they|he|she)\b[^.!?\n]{0,80}"
    r"\b(?:said|says|wrote|reported|requested|prefers)\b|"
    r"\b(?:said|says|wrote|reported)\s*:",
    re.I,
)
_REPORT = re.compile(
    r"\b(?:said|says|wrote|writes|reported|reports|requested|requests|prefers)\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"[?]|\b(?:if|unless|might|maybe|perhaps|probably|possibly|"
    r"suppose|hypothetical|would|could|when|provided|until|assuming)\b",
    re.I,
)
_CONTEXT = re.compile(
    r"\A(?:for|in|on|within|regarding)\b[^.!?;\n]*?,\s*", re.I
)
_DIRECT = re.compile(
    r"\A(?:I\s+(?:want|prefer|decide|require)\b|"
    r"(?:please|use|always|never|remember)\b)\s+\S",
    re.I,
)
_SENTENCE = re.compile(r".+?(?:[.!;](?=\s|$)|$)", re.S)


def _reported(text: str) -> bool:
    """Reject reporting predicates without making assumptions about names."""
    for match in _REPORT.finditer(text):
        prefix = re.split(r"[.!;\n]", text[: match.start()])[-1].strip()
        prefix = _CONTEXT.sub("", prefix, count=1)
        # In this narrow operand position, "reported" modifies a noun;
        # elsewhere its grammatical role is too ambiguous to grant authority.
        adjective = match[0].lower() == "reported" and re.fullmatch(
            r"(?:use|please use|always use|never use)(?: the)?", prefix, re.I
        )
        if not adjective:
            return True
    return False


def _injected(record: dict[str, Any]) -> bool:
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


def _plain(text: str) -> str | None:
    """Mask code operands while refusing quoted, imported or wrapped prose."""
    # Code operands do not identify a speaker. Mask only a single token;
    # sentences, fences and unmatched delimiters retain their quotation risk.
    plain = _IDENTIFIER.sub(lambda match: "x" * len(match[0]), text)
    if (
        _QUOTED.search(plain)
        or _IMPORTED.search(plain)
        or _reported(plain)
        or any(
            marker in text
            for marker in (
                *HARNESS_TAGS,
                *CLAUDE_HARNESS_PREFIXES,
                CHATGPT_REFERENCE_TAG,
            )
        )
        or re.search(r"(?m)^\s*(?:#|~~~)", plain)
    ):
        return None
    return plain


def _direct(text: str) -> bool:
    """Match an owner clause after an optional contextual adjunct."""
    clause = _CONTEXT.sub("", text.strip(), count=1)
    return "," not in clause and _DIRECT.match(clause) is not None


def direct_speech(
    context: str,
    text: str,
    span: tuple[int, int],
    record: dict[str, Any],
) -> bool:
    """Require safe message context and a direct assertion in the selection.

    Args:
        context: All owner-statement fields from the physical message.
        text: The entire selected field before taking the span.
        span: Original Unicode start and end offsets of the selection.
        record: Provider record containing any origin or injection flags.

    Returns:
        Whether bounded syntax establishes affirmative owner intent after
        provenance exclusions. Unsupported wording remains untrusted.
    """
    plain_context = _plain(context)
    if (
        _injected(record)
        or plain_context is None
        or _UNCERTAIN.search(plain_context)
    ):
        return False
    plain = _plain(text)
    start, end = span
    if plain is None:
        return False
    # Preserve whole enclosing instructions: a slice must not discard a
    # negation, contextual scope, condition, or restriction at either end.
    # Newlines can continue an operand; they do not create independent speech.
    sentences = [
        sentence
        for sentence in _SENTENCE.finditer(plain)
        if sentence.start() < end
        and sentence.end() > start
        and sentence[0].strip(" .!;\t\r\n")
    ]
    return bool(sentences) and all(
        _direct(sentence[0])
        and not plain[sentence.start() : start].strip()
        and not plain[end : sentence.end()].strip(" .!;\t\r\n")
        for sentence in sentences
    )
