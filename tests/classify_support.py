"""Shared synthetic records and builders for the classify test modules.

Every record here is synthetic.  Key names and value types mirror real
Codex rollouts; no transcript text is used.  Claude-specific builders live in
``tests.classify_claude_support``.
"""

from __future__ import annotations

from typing import Any

from pctx import classify as c
from pctx.event_model import Record

TS = "2026-01-02T03:04:05.678Z"
CWD = "/work/repo"
PARENT = "thr-parent"
GUARDIAN_SOURCE = {"subagent": {"other": "guardian"}}

R = "[redacted:secret]"
# Fake secrets, assembled at runtime so no key-shaped literal is committed.
SK = "sk-" + "A1b2C3d4" * 3
GHP = "ghp_" + "Z9y8X7w6" * 3
AKIA = "AKIA" + "QWERTYUIOPASDFGH"
PEM_BODY = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu"

PASSTHROUGH = {"internal_chat_message_metadata_passthrough": {"turn_id": "t1"}}
REPORT = "Message Type: FINAL_ANSWER\nPayload:\nsynthetic report"
TASK = "Message Type: NEW_TASK\nTask name: t1\nPayload:\n"
EXEC_ARGS = '{"cmd":"make test","workdir":"/work/repo"}'
PROC = "Chunk ID: a1\nWall time: 0.1 seconds\nProcess exited with code {}\n"
SCRIPT = "Script {}\nWall time 0.2 seconds\nOutput:\n"


def codex_meta(
    thread_source: object = "user",
    tid: str = "thr-a",
    *,
    ordinal: int = 0,
    **extra: Any,
) -> Record:
    """Build a line-1 ``session_meta`` record.

    Args:
        thread_source: Value of ``thread_source``; any type, so tests can
            probe malformed values.
        tid: Thread id, also used as the session id.
        ordinal: Record ordinal.
        **extra: Payload keys to add or override.

    Returns:
        The record, with optional keys present only when given.
    """
    payload: Record = {
        "id": tid,
        "session_id": tid,
        "timestamp": TS,
        "cwd": CWD,
        "originator": "codex_cli_rs",
        "cli_version": "0.0.0",
        "source": "cli",
        "thread_source": thread_source,
        "model_provider": "openai",
        "history_mode": "synthetic",
        "multi_agent_version": "synthetic",
        "context_window": {},
        "base_instructions": {"text": "synthetic base instructions"},
        "git": {"branch": "main", "commit_hash": "0" * 40},
    }
    payload.update(extra)
    return {
        "timestamp": TS,
        "type": "session_meta",
        "ordinal": ordinal,
        "payload": payload,
    }


def spawn_source(path: str = "/root/worker") -> Record:
    """Build the ``source`` value of a spawned subagent thread."""
    return {
        "subagent": {
            "thread_spawn": {
                "agent_nickname": "worker",
                "agent_path": path,
                "agent_role": "worker",
                "depth": 1,
                "parent_thread_id": PARENT,
            }
        }
    }


def subagent_meta(
    tid: str = "thr-sub", k: int | None = 5, **extra: Any
) -> Record:
    """Build the ``session_meta`` record of a subagent thread.

    Args:
        tid: Thread id.
        k: First own ordinal (subagent_history_start_ordinal); None
            builds a thread with no replay marker.
        **extra: Payload keys to add or override.

    Returns:
        The record.
    """
    fields: Record = {
        "session_id": PARENT,
        "parent_thread_id": PARENT,
        "source": spawn_source(),
        "agent_path": "/root/worker",
        "agent_nickname": "worker",
    }
    if k is not None:
        fields.update(subagent_history_start_ordinal=k, forked_from_id=PARENT)
    fields.update(extra)
    return codex_meta("subagent", tid, **fields)


def pem(kind: str = "RSA ") -> str:
    """Return a synthetic PEM private-key block of the given flavour."""
    begin = f"-----BEGIN {kind}PRIVATE KEY-----"
    end = f"-----END {kind}PRIVATE KEY-----"
    return f"{begin}\n{PEM_BODY}\n{PEM_BODY[:20]}==\n{end}"


def item(payload: Record, ordinal: int, kind: str = "response_item") -> Record:
    """Wrap a payload in a rollout line envelope."""
    return {
        "timestamp": TS,
        "type": kind,
        "ordinal": ordinal,
        "payload": payload,
    }


def user_msg(
    ordinal: int, *texts: str, role: str = "user", images: int = 0
) -> Record:
    """Build a Codex message record whose blocks are ``texts``.

    Args:
        ordinal: Record ordinal.
        *texts: One ``input_text`` block per text.
        role: Message role.
        images: Number of image blocks (with placeholders) to prepend.

    Returns:
        The record.
    """
    blocks: list[Record] = [{"type": "input_text", "text": t} for t in texts]
    for n in range(images):
        blocks[:0] = [
            {"type": "input_text", "text": f"<image name=[Image #{n + 1}]>"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            {"type": "input_text", "text": "</image>"},
        ]
    payload = {"type": "message", "id": "m", "role": role, "content": blocks}
    return item(dict(payload, **PASSTHROUGH), ordinal)


def reply(ordinal: int, text: str) -> Record:
    """Build a final-answer assistant message record."""
    payload = {
        "type": "message",
        "id": "m",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}],
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def function_call(
    ordinal: int, name: str, arguments: str, call_id: str = "call-1"
) -> Record:
    """Build a ``function_call`` record."""
    payload = {
        "type": "function_call",
        "id": "fc",
        "name": name,
        "namespace": "synthetic",
        "arguments": arguments,
        "call_id": call_id,
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def custom_call(
    ordinal: int, name: str, text: str, call_id: str = "call-1"
) -> Record:
    """Build a ``custom_tool_call`` record."""
    payload = {
        "type": "custom_tool_call",
        "id": "ctc",
        "name": name,
        "input": text,
        "call_id": call_id,
        "status": "completed",
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def turn_context(ordinal: int, cwd: str) -> Record:
    """Build a ``turn_context`` record that sets the working directory."""
    payload = {"cwd": cwd, "turn_id": "t1", "model": "m", "effort": "high"}
    return item(payload, ordinal, "turn_context")


def agent_message(
    ordinal: int,
    author: str,
    recipient: str,
    text: str,
    encrypted: bool = False,
) -> Record:
    """Build an inter-agent ``agent_message`` record.

    Args:
        ordinal: Record ordinal.
        author: Sending agent path.
        recipient: Receiving agent path.
        text: Message text.
        encrypted: Append an encrypted block next to the text.

    Returns:
        The record.
    """
    content: list[Record] = [{"type": "input_text", "text": text}]
    if encrypted:
        content.append(
            {"type": "encrypted_content", "encrypted_content": "gAAA"}
        )
    payload = {
        "type": "agent_message",
        "id": "am",
        "author": author,
        "recipient": recipient,
        "content": content,
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def fc_output(ordinal: int, call_id: str, output: object) -> Record:
    """Build a ``function_call_output`` record."""
    payload = {
        "type": "function_call_output",
        "call_id": call_id,
        "id": "o",
        "output": output,
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def ctc_output(ordinal: int, call_id: str, text: object) -> Record:
    """Build a ``custom_tool_call_output`` record with one text block."""
    payload = {
        "type": "custom_tool_call_output",
        "call_id": call_id,
        "id": "o",
        "output": [{"type": "input_text", "text": text}],
    }
    return item(dict(payload, **PASSTHROUGH), ordinal)


def run_codex(records: list[Record]) -> tuple[list[c.EventRec], c.CodexState]:
    """Feed ``records`` through one Codex state, numbering lines from 1.

    Returns:
        All emitted events and the final state.
    """
    state = c.CodexState()
    events: list[c.EventRec] = []
    for line, record in enumerate(records, start=1):
        events += c.codex_events(record, line, state)
    return events, state


def ev(
    line: int,
    kind: str,
    text: str,
    *,
    role: str = "user",
    tag: str | None = None,
    flags: int = 0,
    seq: int | None = None,
    **kw: Any,
) -> c.EventRec:
    """Build the expected event of a Codex line.

    Args:
        line: One-based line number.
        kind: Event kind.
        text: Event text.
        role: Event role.
        tag: Event tag.
        flags: Event flags.
        seq: Sequence; defaults to the zero-based line position.
        **kw: ``part`` (default 1) and ``call_id``.

    Returns:
        The event.
    """
    return c.EventRec(
        line=line,
        part=kw.pop("part", 1),
        seq=line - 1 if seq is None else seq,
        ts=TS,
        role=role,
        kind=kind,
        tag=tag,
        flags=flags,
        text=text,
        **kw,
    )


def exec_errors(output: object, *, custom: bool = False) -> list[c.EventRec]:
    """Return the ``tool_error`` events of one Codex exec call and output.

    Args:
        output: Output payload of the call.
        custom: Use a custom tool call instead of a function call.

    Returns:
        Only the ``tool_error`` events.
    """
    if custom:
        records = [
            custom_call(1, "exec", "await tools.x()", "c1"),
            ctc_output(2, "c1", output),
        ]
    else:
        records = [
            function_call(1, "exec_command", EXEC_ARGS, "c1"),
            fc_output(2, "c1", output),
        ]
    events, _ = run_codex([codex_meta(), *records])
    return [e for e in events if e.kind == "tool_error"]


def terr(
    line: int,
    text: str,
    *,
    tag: str = "exec_command",
    flags: int = 0,
    call_id: str = "c1",
    part: int = 1,
) -> c.EventRec:
    """Build the expected ``tool_error`` event of a Codex line."""
    return c.EventRec(
        line,
        part,
        line - 1,
        TS,
        "user",
        "tool_error",
        tag,
        flags,
        text,
        call_id,
    )
