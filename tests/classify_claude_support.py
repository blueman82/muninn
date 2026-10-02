"""Claude transcript builders for the classify test modules.

Every record here is synthetic; key names mirror real Claude transcripts.
"""

from __future__ import annotations

from typing import Any

from muninn import classify as c
from muninn.event_model import Record
from tests.classify_support import CWD, TS

SESSION = "sess-1"
MAIN = f"-work-repo/{SESSION}.jsonl"
SUB = f"-work-repo/{SESSION}/subagents/agent-a1.jsonl"


def claude_rec(
    rtype: str, content: object, *, sidechain: bool = False, **extra: Any
) -> Record:
    """Build a Claude user/assistant record with the real top-level keys.

    Args:
        rtype: ``user`` or ``assistant``.
        content: Message content, a string or a list of blocks.
        sidechain: Mark the record as a subagent sidechain record.
        **extra: Top-level keys to add or override.

    Returns:
        The record.
    """
    message: Record = {"role": rtype, "content": content}
    record: Record = {
        "parentUuid": None,
        "isSidechain": sidechain,
        "userType": "external",
        "cwd": CWD,
        "sessionId": SESSION,
        "version": "0.0.0",
        "gitBranch": "main",
        "entrypoint": "cli",
        "type": rtype,
        "uuid": "u1",
        "timestamp": TS,
        "message": message,
    }
    if rtype == "user":
        record["promptId"] = "p1"
    else:
        record.update(requestId="req-1", apiBlockIndex=0, effort="high")
        message.update(
            id="msg-1",
            type="message",
            model="synthetic",
            stop_reason="end_turn",
            stop_sequence=None,
            usage={},
        )
    if sidechain:
        record["agentId"] = "a1"
    record.update(extra)
    return record


def text_block(text: str) -> Record:
    """Build a Claude ``text`` content block."""
    return {"type": "text", "text": text}


def tool_use(name: str, tool_input: Record, use_id: str = "toolu_1") -> Record:
    """Build a Claude ``tool_use`` content block."""
    return {
        "type": "tool_use",
        "id": use_id,
        "name": name,
        "input": tool_input,
        "caller": {"type": "direct"},
    }


def tool_result(
    use_id: str, content: object, is_error: bool | None = None
) -> Record:
    """Build a Claude ``tool_result`` block; ``is_error`` only when given."""
    block: Record = {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": content,
    }
    if is_error is not None:
        block["is_error"] = is_error
    return block


def bash(command: str, use_id: str = "toolu_9") -> Record:
    """Build an assistant record that runs one Bash command."""
    return claude_rec(
        "assistant", [tool_use("Bash", {"command": command}, use_id)]
    )


def cev(
    kind: str,
    text: str,
    *,
    role: str = "user",
    tag: str | None = None,
    flags: int = 0,
    part: int = 1,
    **kw: Any,
) -> c.EventRec:
    """Build the expected event of the Claude record on line 7.

    Args:
        kind: Event kind.
        text: Event text.
        role: Event role.
        tag: Event tag.
        flags: Event flags.
        part: Index of the event within its line.
        **kw: ``call_id``.

    Returns:
        The event.
    """
    return c.EventRec(7, part, 7, TS, role, kind, tag, flags, text, **kw)


def run_claude(records: list[Record], state: bool = True) -> list[c.EventRec]:
    """Feed ``records`` through ``claude_events``, numbering lines from 1.

    Args:
        records: Parsed transcript lines.
        state: Share one parse state, which enables ``tool_error`` events.

    Returns:
        All emitted events.
    """
    parse_state = c.CodexState() if state else None
    events: list[c.EventRec] = []
    for line, record in enumerate(records, start=1):
        events += c.claude_events(record, line, parse_state)
    return events
