"""Human-facing hook status that accompanies the untrusted memory block."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from muninn.hook_banner import Item, item_lines
from muninn.hook_frame import frame
from muninn.knowledge_models import KnowledgeContext


def learning_status() -> str:
    """Render the fixed notice shown while learning is off."""
    return (
        "Muninn learning is off. Run muninn learn on to turn it on, "
        "or muninn doctor --pretty to check."
    )


def status_line(
    event: str, trace: Mapping[str, object], shown: list[str]
) -> str:
    """Describe loaded SessionStart context or report a hook error.

    Args:
        event: Provider hook event name.
        trace: Content-free result of building the context.
        shown: The sanitized knowledge entries included in the block.

    Returns:
        The status line, or an empty string for prompt recall.
    """
    if "error" in trace:
        return f"muninn: memory unavailable ({trace['error']})"
    if event != "SessionStart":
        return ""
    omitted = bool(trace.get("knowledge_omitted"))
    if not shown:
        status = (
            "knowledge omitted by byte limit"
            if omitted
            else "no eligible knowledge displayed"
        )
        return f"muninn: memory loaded ({status})"
    head = f"muninn: memory loaded ({len(shown)} knowledge entries)"
    if omitted:
        head += "; some knowledge omitted by byte limit"
    return "\n".join([head, *shown])


def start_trace(
    shown: list[Item], context: KnowledgeContext, trace: dict[str, object]
) -> None:
    """Record the shown memory codes and lines of a session-start block.

    Args:
        shown: The memories the block shows, in order.
        context: Every eligible memory the block was cut from.
        trace: Receives ``knowledge_ids``, ``knowledge_omitted`` and the
            ``shown`` lines for the human status message.
    """
    trace["knowledge_ids"] = [item.ident for item in shown]
    trace["knowledge_omitted"] = context.truncated or len(shown) < (
        len(context.notes) + len(context.guides)
    )
    trace["shown"] = [
        printable(f"{item.ident} {item_lines(number, item)[0]}")
        for number, item in enumerate(shown, 1)
    ]


def printable(text: str) -> str:
    """Replace control characters so stored text cannot drive a terminal."""
    return "".join(c if c.isprintable() else " " for c in text)


def failure_notice(code: str) -> str:
    """The block a provider sees when the store cannot be read."""
    label = (
        "context unavailable. The exact detail is not recorded"
        if code == "context_unavailable"
        else (
            "learning policy unavailable"
            if code == "review_policy_required"
            else "store unavailable"
        )
    )
    return frame(' kind="notice"', [f"muninn: {label} ({code})"])


def store_reason(message: str) -> str:
    """Map exact known store failures without exposing arbitrary text."""
    known = {
        "cutover_required": "cutover_required",
        "store access control is unsafe or absent": "store_access_refused",
        "cannot read the store (SQLITE_CANTOPEN)": "store_open_failed",
        "cannot read the store (SQLITE_NOTADB)": "store_corrupt",
        "cannot read the store (SQLITE_CORRUPT)": "store_corrupt",
        "cannot read the store (SQLITE_IOERR)": "store_read_failed",
    }
    return known.get(message, "store_detail_unknown")


def scope_label(conn: sqlite3.Connection, ids: list[int], cwd: str) -> str:
    """Name the scope: its stored label, else the last path component."""
    row = conn.execute(
        "SELECT label FROM scope WHERE kind != 'global' AND id IN"
        f" ({','.join('?' * len(ids))}) LIMIT 1",
        ids,
    ).fetchone()
    # Not Path.name: a trailing slash must give the whole cwd.
    return row[0] if row else cwd.rpartition("/")[2] or cwd
