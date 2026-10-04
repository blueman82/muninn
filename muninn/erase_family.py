"""Erase planning for fork families: where else a line's content is stored.

A fork copies its parent's history.  New reads do not store that replayed
prefix, but a fork read before its parent grew, or before the replay rule
existed, can hold copies, so erase finds them.  Only the leading run of a
fork's events that repeats its ancestors' history counts as a copy, which
is the rule ingest applies; a later identical turn of the fork's own stays.
"""

from __future__ import annotations

import sqlite3

from muninn.tombstones import role_digest

__all__ = ["copies_of"]


def _fork_sources(
    conn: sqlite3.Connection, source: sqlite3.Row
) -> list[sqlite3.Row]:
    """Return every source forked, directly or not, from a source's thread."""
    seen = {source["thread_id"]}
    frontier = set(seen)
    found: list[sqlite3.Row] = []
    while frontier:  # terminates: a thread already seen is not revisited
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND forked_from_id IN"
            f" ({marks})",
            (source["provider"], *frontier),
        ).fetchall()
        rows = [r for r in rows if r["thread_id"] not in seen]
        found += rows
        frontier = {r["thread_id"] for r in rows}
        seen |= frontier
    return found


def _ancestors(
    conn: sqlite3.Connection, source: sqlite3.Row
) -> list[sqlite3.Row]:
    """Return the sources a source's thread was forked from, nearest first."""
    seen = {source["thread_id"]}
    found: list[sqlite3.Row] = []
    parent = source["forked_from_id"]
    while parent and parent not in seen:  # a loop ends the walk
        seen.add(parent)
        row = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND thread_id = ?",
            (source["provider"], parent),
        ).fetchone()
        if row is None:
            break
        found.append(row)
        parent = row["forked_from_id"]
    return found


def _replayed_run(
    conn: sqlite3.Connection, member: sqlite3.Row
) -> list[tuple[int, str, str]]:
    """Return a fork's leading events that repeat its ancestors' history.

    Args:
        conn: Read connection.
        member: A source that may be a fork.

    Returns:
        ``(line, role, text)`` of the events before the first one that no
        ancestor holds; empty for a source with no stored ancestor.
    """
    known: set[bytes] = set()
    for ancestor in _ancestors(conn, member):
        known |= {
            role_digest(r[0], r[1])
            for r in conn.execute(
                "SELECT role, text FROM event WHERE source_id = ?",
                (ancestor["id"],),
            )
        }
    run: list[tuple[int, str, str]] = []
    for line, role, text in conn.execute(
        "SELECT line, role, text FROM event WHERE source_id = ?"
        " ORDER BY line, part",
        (member["id"],),
    ):
        if role_digest(role, text) not in known:
            break
        run.append((line, role, text))
    return run


def copies_of(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    pairs: set[tuple[str, str]],
) -> list[tuple[sqlite3.Row, int]]:
    """Find the lines of other family members that copy erased content.

    An ancestor holds the original, so its matching events all count.  A
    descendant or sibling counts only for the part of its leading run that
    repeats history, so its own later identical turn is kept.  An
    unrelated session is never part of the family, and a ``forked_from_id``
    loop ends the walks.

    Args:
        conn: Read connection.
        source: The source whose line is being erased.
        pairs: ``(role, text)`` of the erased events.

    Returns:
        ``(member, line)`` for each copy, without the source itself.
    """
    ancestors = _ancestors(conn, source)
    older = {r["id"] for r in ancestors}
    related = {r["id"]: r for r in ancestors}
    for member in [source, *ancestors]:
        related.update((r["id"], r) for r in _fork_sources(conn, member))
    related.pop(source["id"], None)
    found: list[tuple[sqlite3.Row, int]] = []
    for member in related.values():
        if member["id"] in older:
            lines = {
                r[0]
                for role, text in pairs
                for r in conn.execute(
                    "SELECT line FROM event WHERE source_id = ?"
                    " AND role = ? AND text = ?",
                    (member["id"], role, text),
                )
            }
        else:
            lines = {
                line
                for line, role, text in _replayed_run(conn, member)
                if (role, text) in pairs
            }
        found += [(member, line) for line in sorted(lines)]
    return found
