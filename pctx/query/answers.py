"""Small helpers that shape one answer: error dicts, refs, previews."""

from __future__ import annotations

import sqlite3
from typing import Any

from pctx.query.constants import NOTICE

# Answers are plain JSON-serialisable dicts whose shape varies per query.
type Answer = dict[str, Any]

PREVIEW = 200  # chars of one event preview


class BadArgumentError(Exception):
    """Input the query functions refuse; str() is the error code."""


def error(code: str) -> Answer:
    """Return the standard refusal answer for an error code."""
    return {"error": code, "notice": NOTICE}


def event_ref(row: sqlite3.Row) -> str:
    """Return the provider:thread_id:line.part REF of an event row."""
    return f"{row['provider']}:{row['thread_id']}:{row['line']}.{row['part']}"


def answer_citable(row: sqlite3.Row) -> bool:
    """Say whether the event may support an answer (not ledger validity)."""
    return bool(
        row["thread_class"] == "primary"
        and row["kind"] in ("prompt", "reply")
        and not row["flags"] & 1
    )


def preview(text: str) -> str:
    """Collapse whitespace and cut the text to one preview line."""
    return " ".join(text.split())[:PREVIEW]
