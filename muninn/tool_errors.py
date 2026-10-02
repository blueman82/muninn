"""Linking tool calls to their outputs and keeping only error output.

Tool output is bulky and often sensitive, so only the output of a known,
clean call is stored, and only when it reports an error or a test summary.
"""

from __future__ import annotations

import json
import re

from muninn.event_model import (
    FLAG_MARKER,
    FLAG_MARKERS,
    FLAG_REDACTED,
    FLAG_TRUNCATED,
    CodexState,
    EventRec,
    Origin,
    non_empty_str,
)
from muninn.redaction import redact

# Tools that only poll or control other work: their calls carry no content.
# Only ``wait`` output can be stored, as the continuation of its exec call;
# the other tools' outputs are never stored.
WAIT_TOOLS = frozenset(
    {"wait", "wait_agent", "list_agents", "interrupt_agent", "sleep"}
)
# Half of the budget kept from each end of an oversized error output.
TOOL_ERROR_HALF = 2 * 1024
# Calls that touch transcript stores are "tainted": echoing their output back
# into memory would feed the store its own contents.
TRANSCRIPT_ROOTS = (
    ".codex/sessions",
    ".codex/archived_sessions",
    ".claude/projects",
)
# An output is stored only when error-bearing or a test summary.
TOOL_ERROR_PATTERNS = (
    # traceback: Python tracebacks, Rust and Go panics
    re.compile(
        r"^(?:Traceback \(most recent call last\):"
        r"|thread '[^'\n]*' panicked at |panic: )",
        re.M,
    ),
    # FAILED: pytest, unittest, jest and go test failure lines; TAP
    re.compile(r"^(?:FAIL(?:ED)?\b|--- FAIL\b|not ok \d)", re.M),
    # fatal: git, compilers, CPython
    re.compile(r"^(?:fatal|FATAL|Fatal Python error)\b|: fatal error\b", re.M),
    # error: CLI, compiler and linter diagnostics, exception lines, make,
    # shells
    re.compile(
        r"^(?:error(?:\[\w+\])?:|Error: |ERROR\b|npm (?:ERR!|error) "
        r"|(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception): "
        r"|\S+:\d+(?::\d+)?: (?:error|fatal error)\b|\S+\(\d+,\d+\): error "
        r"|make(?:\[\d+\])?: \*\*\* )|\bcommand not found\b",
        re.M,
    ),
    # test summary: pytest, unittest, jest and vitest, cargo, go test
    re.compile(
        r"^=*[ \t\r]*(?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed"
        r"|deselected|warnings?),? )+in [\d.]+s\b"
        r"|^Ran \d+ tests? in [\d.]+s"
        r"|^[ \t\r]*Tests?:?[ \t]+(?:\d+ \w+, )*\d+ (?:passed|failed|total)\b"
        r"|^test result: |^ok[ \t]+\S+[ \t]+[\d.]+s\r?$",
        re.M,
    ),
)
# The command word muninn (or a path to it, after env assignments) at a command
# position, or ``python -m muninn``. A muninn call reads the store, so its
# output must not be stored back. The env-assignment run is bounded and
# possessive so hostile input cannot make it backtrack.
MUNINN_CALL = re.compile(
    r"(?:^|[;&|(`'\"]|\$\()[ \t]*(?:\w+=\S{0,256}+[ \t]+)*"
    r"(?:[^\s;&|()`'\"]*/)?muninn(?![\w./:-])|\s-m\s+muninn\b",
    re.M,
)
EXIT_CODE = re.compile(
    r"^(?:Process exited with code|Exit code:?) (-?\d+)"
    r"|\bexit_code\"?\s*[=:]\s*(-?\d+)",
    re.M,
)
# A Codex or Claude transcript line inside an output: a nested transcript is
# never stored.
_NESTED = re.compile(
    r"\"type\"\s*:\s*\"(?:session_meta|response_item|event_msg"
    r"|turn_context)\"|\"parentUuid\"\s*:"
)
_RUNNING = re.compile(r"Script running with cell ID (\S+)")


def _cell(body: str) -> str | None:
    """Return the ``cell_id`` of a JSON tool body, or None if unreadable."""
    try:
        return non_empty_str(json.loads(body).get("cell_id"))
    except (ValueError, AttributeError, RecursionError):
        return None


def register_call(
    state: CodexState | None, call_id: str | None, name: str, body: str
) -> int:
    """Remember a call so its output can be linked later.

    Mutates ``state``: stores the call in ``state.calls`` and, for a ``wait``
    continuation, moves its exec cell entry from ``state.cells`` into
    ``state.calls``.

    Args:
        state: Per-source state; None skips linking.
        call_id: Provider call id, if the record has one.
        name: Tool name.
        body: Serialised call arguments.

    Returns:
        The event flags for the call: ``FLAG_MARKER`` when it invokes muninn.
    """
    muninn = MUNINN_CALL.search(body) is not None
    if state is not None and call_id:
        if name == "wait":  # an exec cell continuation: the exec call owns it
            cell = _cell(body)
            if cell in state.cells:
                state.calls[call_id] = state.cells.pop(cell)
        elif name not in WAIT_TOOLS:
            tainted = muninn or any(root in body for root in TRANSCRIPT_ROOTS)
            state.calls[call_id] = (call_id, None if tainted else name)
    # ponytail: write_stdin continuations are not linked to their
    # exec_command; content markers still apply to their outputs.
    return FLAG_MARKER if muninn else 0


def is_error_text(text: str) -> bool:
    """Return True when tool output reports a failure or a test summary."""
    if text.startswith("Script failed"):  # the exec tool's failure status
        return True
    # Group 1 or 2 holds the code depending on which alternative matched.
    if any(int(a or b) != 0 for a, b in EXIT_CODE.findall(text)):
        return True
    return any(p.search(text) for p in TOOL_ERROR_PATTERNS)


def _excerpt(text: str) -> tuple[str, bool]:
    """Keep the head and tail of an oversized output; flag whether cut."""
    data = text.encode("utf-8", "surrogatepass")
    if len(data) <= 2 * TOOL_ERROR_HALF:
        return text, False
    # Errors and summaries sit at the ends of an output; the middle is noise.
    head = data[:TOOL_ERROR_HALF].decode("utf-8", "ignore")
    tail = data[-TOOL_ERROR_HALF:].decode("utf-8", "ignore")
    return f"{head}\n…\n{tail}", True


def tool_error(
    state: CodexState | None,
    call_id: str | None,
    text: str,
    origin: Origin,
    *,
    is_error: bool = False,
) -> list[EventRec]:
    """Build a ``tool_error`` event for the output of a known, clean call.

    Mutates ``state``: always consumes the pending ``state.calls`` entry for
    ``call_id``, even when it returns an empty list, and records a
    ``state.cells`` entry when the output says the exec cell is still
    running.

    Args:
        state: Per-source state holding the pending calls.
        call_id: Id of the call this output answers.
        text: The tool output.
        origin: Position of the output record.
        is_error: True when the provider already marked the output an error.

    Returns:
        One event for an error-bearing or test-summary output; an empty list
        for everything else, including unknown, tainted or nested output.
    """
    unknown: tuple[None, None] = (None, None)
    origin_call, tool = (
        state.calls.pop(call_id, unknown)
        if state is not None and call_id
        else unknown
    )
    running = _RUNNING.match(text)
    if state is not None and origin_call and running:
        # Later wait(cell_id) outputs belong to the same originating call.
        state.cells[running.group(1)] = (origin_call, tool)
    if tool is None or not (is_error or is_error_text(text)):
        return []
    if _NESTED.search(text) or any(m in text for m in FLAG_MARKERS):
        return []  # a nested transcript or envelope, never stored
    text, changed = redact(text)
    text, cut = _excerpt(text)
    flags = (FLAG_REDACTED if changed else 0) | (FLAG_TRUNCATED if cut else 0)
    return [
        EventRec(
            origin.line,
            origin.part,
            origin.seq,
            origin.ts,
            "user",
            "tool_error",
            tool,
            flags,
            text,
            origin_call,
        )
    ]
