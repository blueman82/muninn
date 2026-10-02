"""Classification of Claude transcript records into thread facts and events."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from muninn.event_model import (
    TOOL_CALL_LIMIT,
    CodexState,
    Draft,
    EventRec,
    Origin,
    Record,
    ThreadInfo,
    as_list,
    as_record,
    join_text,
    make_event,
    non_empty_str,
    starting_tag,
    user_kind,
)
from muninn.tool_errors import register_call, tool_error

# Input the owner types while Claude works is stored as an attachment of this
# type; only the "human" origin is the owner, the rest are system notices,
# other sessions or coordinators.
QUEUED_COMMAND_TYPE = "queued_command"
OWNER_ORIGIN_KIND = "human"
# A person in a chat channel can also carry the "human" origin; that is not
# the owner typing into this session.
CHANNEL_HUMAN_FLAG = "verifiedSlackHumanTurn"
# Set by the harness on text it injected; user records carry it on the record,
# queued input on the attachment.
META_FLAG = "isMeta"

# A Claude user text starting with one of these was written by the harness,
# not typed by the person.
CLAUDE_HARNESS_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<system-reminder>",
    "[Request interrupted",
    "Caveat:",
    "Base directory for this skill",
    "<task-notification>",
    "This session is being continued",
)


def claude_thread(rel_path: str, first: Record) -> ThreadInfo:
    """Classify a Claude transcript from its path and first record.

    A main file's thread id is its ``sessionId``, which equals the file
    stem. A ``<session>/subagents/`` file's thread id is its own file stem and
    its session is the ``sessionId``, falling back to the ``<session>``
    directory name (the parent of ``subagents/``). A file outside such a
    directory whose first record has ``isSidechain`` true is also a subagent.
    When no ``sessionId`` is present the session falls back to the file stem.

    Args:
        rel_path: Path of the transcript under the projects root.
        first: The first parsed record of the file.

    Returns:
        Identity and class of the thread; Claude threads never replay.
    """
    path = PurePosixPath(rel_path)
    session = non_empty_str(first.get("sessionId"))
    if "subagents" in path.parts[:-1]:
        cls, reason = "subagent", "path:subagents"
        session = session or path.parent.parent.name
    elif first.get("isSidechain") is True:
        cls, reason = "subagent", "isSidechain"
    else:
        cls, reason = "primary", "path:main"
    session = session or path.stem
    thread_id = path.stem if cls == "subagent" else session
    parent = session if session != thread_id else None
    return ThreadInfo(
        "claude", thread_id, session, parent, None, cls, reason, "none", None
    )


def _claude_text(content: object) -> str:
    """Return string content as is, or the joined text blocks.

    Thinking and tool blocks are excluded because only visible text is
    searchable.
    """
    return content if isinstance(content, str) else join_text(content)


def _assistant_events(
    content: object,
    blocks: list[Any],
    origin: Origin,
    state: CodexState | None,
) -> list[EventRec]:
    """Events of an assistant record: its reply, then one per tool call."""
    drafts: list[Draft] = []
    text = _claude_text(content)
    if text.strip():
        drafts.append(Draft("reply", None, text))
    for item in blocks:
        block = as_record(item)
        if block is not None and block.get("type") == "tool_use":
            name = non_empty_str(block.get("name")) or "unknown"
            body = json.dumps(block.get("input"), ensure_ascii=False)
            call_id = non_empty_str(block.get("id"))
            flags = register_call(state, call_id, name, body)
            drafts.append(
                Draft(
                    "tool_call",
                    name,
                    f"{name}: {body}",
                    TOOL_CALL_LIMIT,
                    flags,
                    call_id,
                )
            )
    # Several events can share a line; ``part`` keeps their identity unique.
    return [
        make_event(
            Origin(origin.line, origin.seq, origin.ts, part),
            "assistant",
            draft,
        )
        for part, draft in enumerate(drafts, start=1)
    ]


def _result_events(
    results: list[Record], origin: Origin, state: CodexState | None
) -> list[EventRec]:
    """Events for the ``tool_result`` blocks of a user record."""
    events: list[EventRec] = []
    for block in results:
        part = Origin(origin.line, origin.seq, origin.ts, len(events) + 1)
        events += tool_error(
            state,
            non_empty_str(block.get("tool_use_id")),
            _claude_text(block.get("content")),
            part,
            is_error=block.get("is_error") is True,
        )
    return events


def _user_events(
    record: Record, content: object, origin: Origin
) -> list[EventRec]:
    """Events for a user record that carries no tool results."""
    text = _claude_text(content)
    if not text.strip():
        return []
    tag = starting_tag(text, CLAUDE_HARNESS_PREFIXES)
    for flag in ("isCompactSummary", META_FLAG):
        if tag is None and record.get(flag) is True:
            tag = flag
    thread_class = (
        "subagent" if record.get("isSidechain") is True else "primary"
    )
    kind = user_kind(tag, thread_class)
    return [make_event(origin, "user", Draft(kind, tag, text))]


def _queued_events(record: Record, origin: Origin) -> list[EventRec]:
    """Events for an attachment: only the owner's mid-turn typed prompt.

    Task notices, peer sessions and coordinators also arrive as queued
    commands but are not the owner speaking, so they stay unindexed.

    Args:
        record: The attachment record.
        origin: Position and timestamp of the record.

    Returns:
        One user event (normally a prompt) for the owner's input, otherwise
        nothing.
    """
    att = as_record(record.get("attachment"))
    if att is None or att.get("type") != QUEUED_COMMAND_TYPE:
        return []
    source = as_record(att.get("origin"))
    if source is None or source.get("kind") != OWNER_ORIGIN_KIND:
        return []
    if att.get(CHANNEL_HUMAN_FLAG) is True:
        return []
    # The meta flag sits on the attachment here, not on the record, so lift
    # it to where ordinary user records carry it.
    meta = {META_FLAG: True} if att.get(META_FLAG) is True else {}
    lifted = record | meta
    return _user_events(lifted, att.get("prompt"), origin)


def claude_events(
    record: Record, line: int, state: CodexState | None = None
) -> list[EventRec]:
    """Return the events of one Claude transcript record.

    Args:
        record: The parsed transcript line.
        line: One-based line number of the record.
        state: One state per source enables ``tool_error`` events; an output
            is stored only when its ``tool_use`` was seen in the same source.

    Returns:
        Zero or more events; empty for records that carry no stored text.
    """
    rtype, message = record.get("type"), as_record(record.get("message"))
    origin = Origin(line, line, non_empty_str(record.get("timestamp")))
    if rtype == "attachment":
        return _queued_events(record, origin)
    if rtype not in ("user", "assistant") or message is None:
        return []  # system, summary, titles, queue-operation ...
    if message.get("role") not in (rtype, None):
        return []
    content = message.get("content")
    blocks = as_list(content) or []
    if rtype == "assistant":
        return _assistant_events(content, blocks, origin, state)
    results = [
        b
        for b in map(as_record, blocks)
        if b is not None and b.get("type") == "tool_result"
    ]
    if results or "toolUseResult" in record:  # tool output, never a prompt
        return _result_events(results, origin, state)
    return _user_events(record, content, origin)
