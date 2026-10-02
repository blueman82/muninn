"""Pure classification of Codex and Claude transcript records.

No I/O: callers pass parsed records and receive thread facts and events. The
work is split by responsibility into ``redaction`` (secret spans),
``event_model`` (shared types and builders), ``tool_errors`` (linking calls
to error output), ``codex_events`` and ``claude_events`` (one provider
each). This module keeps the small shared helpers and re-exports the public
surface that the rest of muninn and the tests import from here.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from muninn.claude_events import (
    CLAUDE_HARNESS_PREFIXES,
    claude_events,
    claude_thread,
)
from muninn.codex_events import HARNESS_TAGS, codex_events, codex_thread
from muninn.event_model import (
    FLAG_MARKER,
    FLAG_MARKERS,
    FLAG_REDACTED,
    FLAG_TRUNCATED,
    INJECTED_MARKERS,
    NOTICE,
    TEXT_LIMIT,
    TOOL_CALL_LIMIT,
    CodexState,
    EventRec,
    Record,
    ThreadInfo,
    as_record,
    non_empty_str,
)
from muninn.event_model import join_text as _join
from muninn.redaction import REDACTED, SECRET_PATTERNS, redact
from muninn.tool_errors import EXIT_CODE as _EXIT
from muninn.tool_errors import (
    MUNINN_CALL,
    TOOL_ERROR_HALF,
    TOOL_ERROR_PATTERNS,
    TRANSCRIPT_ROOTS,
    WAIT_TOOLS,
)
from muninn.tool_errors import is_error_text as _is_error

# Bump when classification changes in a way that requires re-reading sources.
# 2: owner messages typed mid-turn (queued_command attachments) were not
# indexed before, so old sources must be re-read to gain them.
# 3: Codex chatgpt_handoff sessions were classed other and had no events, so
# they must be re-read to gain them.
CLASSIFIER_VERSION = 3
# Deepest JSON nesting accepted; a hostile line cannot exhaust the stack.
MAX_DEPTH = 1_000

__all__ = [
    "CLASSIFIER_VERSION",
    "CLAUDE_HARNESS_PREFIXES",
    "FLAG_MARKER",
    "FLAG_MARKERS",
    "FLAG_REDACTED",
    "FLAG_TRUNCATED",
    "HARNESS_TAGS",
    "INJECTED_MARKERS",
    "MAX_DEPTH",
    "MUNINN_CALL",
    "NOTICE",
    "REDACTED",
    "SECRET_PATTERNS",
    "TEXT_LIMIT",
    "TOOL_CALL_LIMIT",
    "TOOL_ERROR_HALF",
    "TOOL_ERROR_PATTERNS",
    "TRANSCRIPT_ROOTS",
    "WAIT_TOOLS",
    "_EXIT",
    "CodexState",
    "EventRec",
    "ThreadInfo",
    "_is_error",
    "_join",
    "claude_events",
    "claude_thread",
    "codex_events",
    "codex_thread",
    "cwd_of",
    "record_hash",
    "redact",
    "within_depth",
]


def record_hash(raw_line: bytes) -> str:
    """Hash a JSONL line without its trailing CR/LF.

    The hash is the line's identity in the store, so a file that gains or
    loses CRLF endings must not look like changed content.

    Args:
        raw_line: The line as read from the file.

    Returns:
        Hex SHA-256 of the line without its line terminator.
    """
    return hashlib.sha256(raw_line.rstrip(b"\r\n")).hexdigest()


def within_depth(value: object) -> bool:
    """Return False for JSON nested deeper than ``MAX_DEPTH``.

    Walks with an explicit stack because recursion would overflow on the
    hostile input this guards against. ``json.loads`` only builds dicts and
    lists, and concrete type checks are several times faster than the
    ``collections.abc`` checks.

    Args:
        value: A value returned by ``json.loads``.

    Returns:
        True when the nesting is within the limit.
    """
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_DEPTH:
            return False
        if isinstance(item, dict):
            children = cast(Record, item).values()
            pending.extend((child, depth + 1) for child in children)
        elif isinstance(item, list):
            pending.extend((c, depth + 1) for c in cast(list[Any], item))
    return True


def cwd_of(record: Record, state: CodexState | None) -> str | None:
    """Return the working directory for a record's events.

    Args:
        record: The parsed transcript line.
        state: Codex state, whose cwd comes from line 1 and from
            ``turn_context`` records after any replay; None for Claude, whose
            records carry their own ``cwd``. A state with no cwd falls back
            to the record's own.

    Returns:
        The working directory, or None when the record has none.
    """
    if state is not None and state.cwd:
        return state.cwd
    payload = as_record(record.get("payload"))
    if payload is not None and record.get("type") in (
        "session_meta",
        "turn_context",
    ):
        return non_empty_str(payload.get("cwd"))
    return non_empty_str(record.get("cwd"))
