"""Collect physical proof lines and fork copies without writing content."""

from __future__ import annotations

import sqlite3

from muninn import decision_erase
from muninn.erase_family import copies_of
from muninn.erase_target import Target
from muninn.knowledge_erase_state import SupportClosure
from muninn.tombstone_intent import IntentRow
from muninn.tombstone_rows import tombstone_row


def add_line(
    conn: sqlite3.Connection,
    target: Target,
    source: sqlite3.Row,
    line: int,
    *,
    copies: bool = True,
) -> None:
    """Add every part of one line; a line tombstone blocks all its parts.

    Keyed content tombstones and fork-family copy selection prevent replay
    from restoring the erased line under another coordinate.

    Args:
        conn: Read connection.
        target: Plan to fill.
        source: The source row that holds the line.
        line: 1-based transcript line.
        copies: Also collect the family's copies of the line's content.
    """
    if (source["id"], line) in target.lines:
        return
    rows = conn.execute(
        "SELECT id, line_sha256, role, text FROM event"
        " WHERE source_id = ? AND line = ?",
        (source["id"], line),
    ).fetchall()
    target.lines.add((source["id"], line))
    target.touched[source["id"]] = source
    target.events += [r[0] for r in rows]
    target.tombstones.append(
        tombstone_row(
            source["provider"],
            "line",
            thread_id=source["thread_id"],
            line=line,
            line_sha256=rows[0][1],
        )
    )
    pairs = {(r[2], r[3]) for r in rows}
    target.content += [
        (source["provider"], source["thread_id"], role, text)
        for role, text in sorted(pairs)
    ]
    if not copies:
        return
    for member, copy in copies_of(conn, source, pairs):
        add_line(conn, target, member, copy, copies=False)


def evidence_lines(
    conn: sqlite3.Connection, target: Target, ids: set[str]
) -> None:
    """Tombstone physical evidence even when it has no indexed event."""
    for ident in sorted(ids):
        row = conn.execute(
            "SELECT * FROM review_evidence WHERE id=?", (ident,)
        ).fetchone()
        target.tombstones.append(
            tombstone_row(
                row["provider"],
                "line",
                thread_id=row["thread_id"],
                line=row["line"],
                line_sha256=row["line_sha256"],
            )
        )
        source = conn.execute(
            "SELECT * FROM source WHERE provider=? AND thread_id=?",
            (row["provider"], row["thread_id"]),
        ).fetchone()
        if source is None or source["id"] in target.sources:
            continue
        lines = conn.execute(
            "SELECT DISTINCT line FROM event WHERE source_id=? AND "
            "line_sha256=?",
            (source["id"], row["line_sha256"]),
        ).fetchall()
        for line in lines:
            add_line(conn, target, source, line[0])


def decision_lines(
    conn: sqlite3.Connection, target: Target, plan: SupportClosure
) -> None:
    """Persist proof coordinates before any evidence was admitted."""
    existing = {IntentRow(**row).key for row in target.tombstones}
    for ident, row in decision_erase.locator_rows(conn):
        if row.line is None:
            raise ValueError("decision_invalid")
        if ident not in plan.decisions or row.key in existing:
            continue
        existing.add(row.key)
        target.tombstones.append(
            tombstone_row(
                row.provider,
                "line",
                thread_id=row.thread_id,
                line=row.line,
                line_sha256=row.line_sha256,
            )
        )
        source = conn.execute(
            "SELECT * FROM source WHERE provider=? AND thread_id=?",
            (row.provider, row.thread_id),
        ).fetchone()
        if (
            source is not None
            and source["id"] not in target.sources
            and conn.execute(
                "SELECT 1 FROM event WHERE source_id=? AND line=? "
                "AND line_sha256=?",
                (source["id"], row.line, row.line_sha256),
            ).fetchone()
        ):
            add_line(conn, target, source, row.line)
