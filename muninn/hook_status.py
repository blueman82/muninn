"""Human-facing hook status that accompanies the untrusted memory block."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from muninn.hook_frame import escape_delimiter, frame
from muninn.knowledge_models import RequestId
from muninn.review_recovery import explain
from muninn.review_recovery_codes import CONTAINED


def learning_status(issue: Mapping[str, str]) -> str:
    """Render plain current wording from safe codes, never saved text."""
    detail = explain(issue.get("code", "config"), "not recorded")
    if issue.get("code") in {
        "control_absent",
        "control_invalid",
        "policy_absent",
        "policy_invalid",
        "policy_digest_mismatch",
    }:
        return (
            "Muninn learning is blocked. "
            "Muninn cannot verify its saved policy. "
            "The agent can inspect the saved setup with "
            "muninn doctor --pretty."
        )
    if issue.get("code") == "paused":
        return (
            "Muninn learning is paused. The agent can inspect its saved"
            " policy."
        )
    closed = issue.get("admission") == "closed"
    action = (
        "Learning remains paused or its saved policy cannot be verified. "
        "The agent can inspect the saved setup with muninn doctor --pretty."
        if closed and detail["code"] in CONTAINED
        else detail["action"]
    )
    message = escape_delimiter(
        printable(f"Muninn learning: {detail['cause']} {action}")
    )
    if closed and detail["code"] not in CONTAINED:
        message += (
            " Learning remains paused or its saved policy cannot be verified."
        )
    try:
        ident = TypeAdapter(RequestId).validate_python(
            issue.get("id"), strict=True
        )
    except ValidationError:
        return message
    return (
        f"{message} The agent can inspect: muninn learn show {ident}. "
        f"Explain: muninn learn explain {ident} --human. "
        f"Dismiss notice: muninn learn dismiss {ident}."
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
