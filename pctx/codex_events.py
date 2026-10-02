"""Classification of Codex rollout records into thread facts and events."""

from __future__ import annotations

import json
import re

from pctx.event_model import (
    TOOL_CALL_LIMIT,
    CodexState,
    Draft,
    EventRec,
    Origin,
    Record,
    ThreadInfo,
    as_record,
    join_text,
    make_event,
    non_empty_str,
    starting_tag,
    user_kind,
)
from pctx.tool_errors import WAIT_TOOLS, register_call, tool_error

# A Codex user text starting with one of these was written by the harness,
# not typed by the person.
HARNESS_TAGS = (
    "<environment_context>",
    "# AGENTS.md instructions",
    "<user_instructions>",
    "<hook_prompt",
    "<subagent_notification>",
    "<turn_aborted>",
    "<recommended_plugins>",
    "<INSTRUCTIONS>",
    "<codex_internal_context",
    "<user_shell_command>",
    "<skill",
    "<task",
)
# Attached images lead a prompt as <image> tags; they are not prompt text.
_IMAGES = re.compile(r"\A\s*(?:<image\b[^>]*>\s*(?:</image>\s*)?)+")


def _codex_class(payload: Record) -> tuple[str, str]:
    """Return the thread class and the reason for it."""
    source = as_record(payload.get("source"))
    sub = as_record(source.get("subagent")) if source is not None else None
    other = sub.get("other") if sub is not None else None
    kind = payload.get("thread_source")
    if kind == "guardian_review":
        return "reviewer", "thread_source=guardian_review"
    if other == "guardian":  # also seen with thread_source=subagent
        return "reviewer", "subagent.other=guardian"
    if kind == "user":
        return "primary", "thread_source=user"
    if kind == "subagent":
        return "subagent", "thread_source=subagent"
    # The value is copied into a stored reason, so cap what a transcript can
    # make us store.
    return "other", f"thread_source={(non_empty_str(kind) or 'missing')[:64]}"


def _replay(payload: Record, forked: str | None) -> tuple[str, int | None]:
    """Return how a thread marks inherited history and where its own starts."""
    start = payload.get("subagent_history_start_ordinal")
    if isinstance(start, int):
        return "ordinal", start
    if payload.get("history_base") is not None:
        return "history_base", None
    if forked:  # old-format user fork; ingest applies the prefix rule
        return "content_prefix", None
    return "none", None


def codex_thread(meta: Record) -> ThreadInfo:
    """Classify a Codex thread from its line-1 ``session_meta`` record.

    Args:
        meta: The parsed first line of the rollout.

    Returns:
        Identity, class and replay facts of the thread.

    Raises:
        ValueError: If line 1 is not a ``session_meta`` record with an id.
    """
    is_meta = meta.get("type") == "session_meta"
    payload = as_record(meta.get("payload")) if is_meta else None
    if payload is None or not non_empty_str(payload.get("id")):
        raise ValueError("line 1 is not a session_meta record with an id")
    thread_id: str = payload["id"]
    forked = non_empty_str(payload.get("forked_from_id"))
    replay_mode, replay_before = _replay(payload, forked)
    vcs = as_record(payload.get("git"))
    commit = non_empty_str(vcs.get("commit_hash")) if vcs else None
    thread_class, class_reason = _codex_class(payload)
    return ThreadInfo(
        "codex",
        thread_id,
        non_empty_str(payload.get("session_id")) or thread_id,
        non_empty_str(payload.get("parent_thread_id")),
        forked,
        thread_class,
        class_reason,
        replay_mode,
        replay_before,
        commit,
    )


def _message(
    payload: Record, origin: Origin, state: CodexState
) -> list[EventRec]:
    """Events for a user or assistant message item."""
    text = join_text(payload.get("content"))
    role = payload.get("role")
    if role == "user":
        text = _IMAGES.sub("", text, count=1)
        tag = starting_tag(text, HARNESS_TAGS)
        kind = user_kind(tag, state.thread_class)
    elif role == "assistant":
        tag, kind = None, "reply"
    else:  # developer and system text is never stored
        return []
    if not text.strip():
        return []
    return [make_event(origin, role, Draft(kind, tag, text))]


def _tool_call(
    payload: Record, origin: Origin, state: CodexState
) -> list[EventRec]:
    """Events for a function or custom tool call item."""
    name = non_empty_str(payload.get("name")) or "unknown"
    is_function = payload.get("type") == "function_call"
    body = payload.get("arguments" if is_function else "input")
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    call_id = non_empty_str(payload.get("call_id"))
    # Register before the wait-tool check: a wait call still has to move its
    # exec cell's ownership even though it stores no event.
    flags = register_call(state, call_id, name, body)
    if name in WAIT_TOOLS:
        return []
    draft = Draft(
        "tool_call", name, f"{name}: {body}", TOOL_CALL_LIMIT, flags, call_id
    )
    return [make_event(origin, "assistant", draft)]


def _agent_message(
    payload: Record, origin: Origin, state: CodexState
) -> list[EventRec]:
    """Return events for a message delivered by another agent.

    The message lives in the recipient's rollout. A report from a descendant
    agent is harness text there; a message from an ancestor into a subagent
    thread is a delegation.

    Args:
        payload: The ``agent_message`` item.
        origin: Position of the record.
        state: Per-source state.

    Returns:
        One event, or none when the message has no text.
    """
    text = join_text(payload.get("content"))
    author = non_empty_str(payload.get("author")) or ""
    to = non_empty_str(payload.get("recipient"))
    report = to is not None and author.startswith(to.rstrip("/") + "/")
    subagent = state.thread_class == "subagent"
    kind = "delegation" if subagent and not report else "harness"
    if not text.strip():
        return []
    return [make_event(origin, "user", Draft(kind, "agent_message", text))]


def _response_item(
    payload: Record, origin: Origin, state: CodexState
) -> list[EventRec]:
    """Dispatch a ``response_item`` payload by its item type."""
    kind = payload.get("type")
    if kind == "message":
        return _message(payload, origin, state)
    if kind in ("function_call", "custom_tool_call"):
        return _tool_call(payload, origin, state)
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        text = output if isinstance(output, str) else join_text(output)
        call_id = non_empty_str(payload.get("call_id"))
        return tool_error(state, call_id, text, origin)
    if kind == "agent_message":
        return _agent_message(payload, origin, state)
    return []


def _is_inherited(state: CodexState, ordinal: object, line: int) -> bool:
    """Return True for a record inherited from the parent's history."""
    if state.replay_before is None:
        return False
    # Replaying threads start at ordinal 0 and advance one per line, so
    # ``line - 1`` stands in when a record carries no ordinal.
    position = ordinal if isinstance(ordinal, int) else line - 1
    return position < state.replay_before


def codex_events(
    record: Record, line: int, state: CodexState
) -> list[EventRec]:
    """Return the events of one Codex rollout record.

    Args:
        record: The parsed rollout line.
        line: One-based line number of the record.
        state: Per-source state; updated as a side effect (cwd, replay).

    Returns:
        Zero or more events; empty for records that carry no stored text.
    """
    rtype, payload = record.get("type"), as_record(record.get("payload"))
    if payload is None:
        return []
    if rtype == "session_meta":
        if line == 1:  # a later session_meta is the parent's copy
            info = codex_thread(record)
            state.cwd = non_empty_str(payload.get("cwd")) or state.cwd
            state.replay_before = info.replay_before
            state.thread_class = info.thread_class
        return []
    ordinal = record.get("ordinal")
    if _is_inherited(state, ordinal, line):
        return []
    if rtype == "turn_context":
        state.cwd = non_empty_str(payload.get("cwd")) or state.cwd
        return []
    if rtype != "response_item":
        return []
    origin = Origin(
        line,
        ordinal if isinstance(ordinal, int) else line,
        non_empty_str(record.get("timestamp")),
    )
    return _response_item(payload, origin, state)
