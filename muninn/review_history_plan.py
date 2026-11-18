"""Plan candidate history windows from the indexed lines of one source."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import NamedTuple

from muninn.review_split import Line, Splitter, safe_limit

_ROWS = (
    "SELECT e.line,e.line_sha256,sc.key,sc.kind,max(e.kind IN "
    "('prompt','reply')) AS candidate,max(e.kind='prompt') AS prompt,"
    "min(e.byte_offset) AS off "
    "FROM event e JOIN scope sc ON sc.id=e.scope_id WHERE e.source_id=? "
    "AND e.line BETWEEN ? AND ? "
    "GROUP BY e.line,sc.id ORDER BY e.line"
)


class Part(NamedTuple):
    """Describe one planned window and how its range was shaped."""

    first: int
    new_from: int
    last: int
    sha: str
    key: str
    cut: bool
    limited: bool


def _sizes(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    lines: dict[int, list[sqlite3.Row]],
    cursor: bool,
) -> dict[int, int]:
    """Charge each line the bytes up to the next indexed line."""
    numbers = list(lines)
    stop = source["cursor_bytes"]
    if numbers and not cursor:
        later = conn.execute(
            "SELECT min(byte_offset) FROM event WHERE source_id=? "
            "AND line>?",
            (source["id"], numbers[-1]),
        ).fetchone()[0]
        stop = stop if later is None else later
    offsets = [lines[n][0]["off"] for n in numbers] + [stop]
    return {
        n: 0 if cursor else offsets[i + 1] - offsets[i]
        for i, n in enumerate(numbers)
    }


def plan(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    max_input_bytes: int,
    project: str | None,
    covered: list[tuple[int, int]],
    bounds: tuple[int, int] | None = None,
) -> list[Part]:
    """Group same-scope indexed lines into windows that fit the limit.

    A JSONL window closes early at the safe byte limit; the window after
    that cut repeats the last owner turn, shortened to fit a new turn.
    Cursor sources are split by the 200-line cap only.

    Args:
        conn: The store connection.
        source: The source row.
        max_input_bytes: The policy read limit.
        project: The selected project, or None for all.
        covered: Line ranges that already have review work.
        bounds: Optional first and last line to plan.

    Returns:
        The windows in order.
    """
    rows = conn.execute(
        _ROWS, (source["id"], *(bounds or (1, 2**63 - 1)))
    ).fetchall()
    lines: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        lines[row["line"]].append(row)
    numbers = list(lines)
    cursor = source["provider"] == "cursor"
    sizes = _sizes(conn, source, lines, cursor)
    prompts = [n for n in numbers if any(r["prompt"] for r in lines[n])]
    safe = 2**62 if cursor else safe_limit(max_input_bytes)
    output: list[Part] = []
    run: list[Line] = []

    def flush() -> None:
        if run:
            splitter = Splitter(run, prompts, source["cursor_line"], safe)
            held = {item.number for item in run}
            for part in splitter.parts():
                first = run[part.first]
                new = run[part.new]
                last = run[part.last]
                if not any(i.candidate for i in run[part.new : part.last + 1]):
                    continue
                low = bisect_left(numbers, first.number - 1)
                high = bisect_right(numbers, last.number)
                high = min(high + 1, len(numbers))
                limited = any(n not in held for n in numbers[low:high])
                output.append(
                    Part(
                        first.number,
                        new.number,
                        last.number,
                        last.sha,
                        first.key,
                        part.cut,
                        limited,
                    )
                )
        run.clear()

    for number in numbers:
        parts = lines[number]
        row = parts[0]
        if (
            len(parts) != 1
            or row["kind"] == "global"
            or (project is not None and row["key"] != project)
            or any(a <= number <= b for a, b in covered)
        ):
            flush()
            continue
        if run and row["key"] != run[0].key:
            flush()
        run.append(
            Line(
                number,
                sizes[number],
                bool(row["candidate"]),
                row["line_sha256"],
                row["key"],
            )
        )
    flush()
    return output
