"""Human-facing hook status that accompanies the untrusted memory block."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from muninn.hook_frame import escape_delimiter
from muninn.knowledge_models import RequestId


def learning_status(issue: Mapping[str, str]) -> str:
    """Render a safe notice with commands only for a valid request ID."""
    message = escape_delimiter(
        printable(
            f"Muninn learning ({issue['stage']}): "
            f"{issue['cause']} {issue['action']}"
        )
    )
    try:
        ident = TypeAdapter(RequestId).validate_python(
            issue["id"], strict=True
        )
    except ValidationError:
        return message
    return (
        f"{message} Inspect: muninn learn show {ident}. "
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
