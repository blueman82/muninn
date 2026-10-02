"""Check that a quotation really occurs in a stored event."""

from __future__ import annotations

import bisect
import re
import sqlite3
from typing import Any

from pctx.query.guard import guarded
from pctx.query.opening import locate


def _find_span(text: str, quote: str) -> list[int] | None:
    """Find a whitespace-collapsed quote and map it back to ``text``."""
    wanted = " ".join(quote.split())
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    starts: list[int] = []  # where each word begins in the collapsed text
    at = 0
    for start, end in words:
        starts.append(at)
        at += end - start + 1
    found = (
        " ".join(text[a:b] for a, b in words).find(wanted) if wanted else -1
    )
    if found < 0:
        return None

    def original(index: int) -> int:
        word = bisect.bisect_right(starts, index) - 1
        return words[word][0] + index - starts[word]

    return [original(found), original(found + len(wanted) - 1) + 1]


@guarded
def quote_check(
    conn: sqlite3.Connection, ref: str, quote: str
) -> dict[str, Any]:
    """Test whether a quote occurs in an event.

    The test is a whitespace-collapsed, case-sensitive substring match; the
    span is in characters of the stored text, whitespace inside it included.

    Args:
        conn: Read-only store connection.
        ref: An event id or REF.
        quote: The text to look for.

    Returns:
        ``{"match": bool, "span": [start, end] | None}``; an unknown ref
        adds ``error``.
    """
    row, problem = locate(conn, ref)
    if row is None:
        return {"match": False, "span": None, "error": problem}
    span = _find_span(row["text"], quote)
    if span is None:
        return {"match": False, "span": None}
    return {"match": True, "span": span}
