"""Erase planning: find everything one erase request removes.

Nothing is written here. The collectors fill a ``Target`` from read-only
queries so ``--dry-run`` can report exact counts and ``erase`` can then apply
the whole plan in one transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from muninn import query
from muninn.tombstones import TombstoneRow, event_tag, key_for, tombstone_row

# Providers blocked when a session has not been ingested yet, so a later
# ingest cannot resurrect it.
_ALL_PROVIDERS = ["codex", "claude"]


@dataclass
class Target:
    """Everything one erase request will remove or list."""

    sessions: set[tuple[str, str]] = field(
        default_factory=set[tuple[str, str]]
    )
    # Source rows are deleted; touched ones (a superset) are only reported.
    sources: dict[int, sqlite3.Row] = field(
        default_factory=dict[int, sqlite3.Row]
    )
    touched: dict[int, sqlite3.Row] = field(
        default_factory=dict[int, sqlite3.Row]
    )
    lines: set[tuple[int, int]] = field(default_factory=set[tuple[int, int]])
    events: list[int] = field(default_factory=list[int])
    tombstones: list[TombstoneRow] = field(default_factory=list[TombstoneRow])
    citations: set[int] = field(default_factory=set[int])
    knowledge: set[int] = field(default_factory=set[int])


def _marks(count: int) -> str:
    """SQL placeholders for ``count`` bound values."""
    return ",".join("?" * count)


def _session_providers(conn: sqlite3.Connection, session: str) -> list[str]:
    """Providers that hold the session, or all of them if none do."""
    found: list[str] = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT provider FROM source WHERE session_root = ?",
            (session,),
        )
    ]
    return found or _ALL_PROVIDERS


def _next_frontier(
    conn: sqlite3.Connection,
    target: Target,
    frontier: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Gather the frontier's sources; return sessions forked from them."""
    found: set[tuple[str, str]] = set()
    for provider, root in frontier:
        rows = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND session_root = ?",
            (provider, root),
        ).fetchall()
        threads = {root} | {r["thread_id"] for r in rows}
        target.sources.update((r["id"], r) for r in rows)
        forks = conn.execute(
            "SELECT DISTINCT provider, session_root FROM source"
            " WHERE provider = ? AND forked_from_id IN"
            f" ({_marks(len(threads))})",
            (provider, *threads),
        )
        found |= {(f[0], f[1]) for f in forks} - target.sessions
    return found


def collect_session(
    conn: sqlite3.Connection, target: Target, session: str
) -> None:
    """Collect a session and every session forked from it, transitively.

    A fork's copied prefix is the erased session's own content, so leaving
    the fork behind would keep the text.

    Args:
        conn: Read connection.
        target: Plan to fill.
        session: Session root id.
    """
    frontier = {(p, session) for p in _session_providers(conn, session)}
    while frontier:  # terminates: next_frontier excludes known sessions
        target.sessions |= frontier
        frontier = _next_frontier(conn, target, frontier)
    target.touched.update(target.sources)
    target.tombstones = [
        tombstone_row(provider, "session", session_root=root)
        for provider, root in sorted(target.sessions)
    ]
    for source_id in target.sources:
        target.events += [
            r[0]
            for r in conn.execute(
                "SELECT id FROM event WHERE source_id = ?", (source_id,)
            )
        ]


def collect_event(conn: sqlite3.Connection, target: Target, ref: str) -> None:
    """Collect the one event line a REF names.

    A malformed ``ref`` makes ``query.parse_ref`` raise ``ValueError``.

    Args:
        conn: Read connection.
        target: Plan to fill.
        ref: ``provider:thread:line.part``; the thread may be a prefix.

    Raises:
        LookupError: If the ref matches no thread, several threads, or no
            event.
    """
    provider, thread, line, part = query.parse_ref(ref)
    sources = conn.execute(
        "SELECT * FROM source WHERE provider = ? AND substr(thread_id, 1, ?)"
        " = ?",
        (provider, len(thread), thread),
    ).fetchall()
    if len(sources) != 1:
        raise LookupError(f"{len(sources)} threads match the ref")
    parts = {
        r[0]
        for r in conn.execute(
            "SELECT part FROM event WHERE source_id = ? AND line = ?",
            (sources[0]["id"], line),
        )
    }
    if part not in parts:
        raise LookupError("no such event")
    _add_line(conn, target, sources[0], line)


def collect_match(
    conn: sqlite3.Connection, target: Target, match: str
) -> None:
    """Collect every event line, knowledge text, reason and quote with text.

    Args:
        conn: Read connection.
        target: Plan to fill.
        match: Literal text to find (no wildcards: ``instr`` is exact).
    """
    hits = conn.execute(
        "SELECT DISTINCT source_id, line FROM event WHERE instr(text, ?) > 0",
        (match,),
    ).fetchall()
    for source_id, line in hits:
        source = conn.execute(
            "SELECT * FROM source WHERE id = ?", (source_id,)
        ).fetchone()
        _add_line(conn, target, source, line)
    target.knowledge |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM knowledge WHERE (text IS NOT NULL"
            " AND instr(text, ?) > 0) OR (retract_reason IS NOT NULL"
            " AND instr(retract_reason, ?) > 0)",
            (match, match),
        )
    }
    target.citations |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM citation WHERE quote IS NOT NULL"
            " AND instr(quote, ?) > 0",
            (match,),
        )
    }


def _fork_sources(
    conn: sqlite3.Connection, source: sqlite3.Row
) -> list[sqlite3.Row]:
    """Return every source forked, directly or not, from a source's thread."""
    seen = {source["thread_id"]}
    frontier = set(seen)
    found: list[sqlite3.Row] = []
    while frontier:  # terminates: a thread already seen is not revisited
        rows = conn.execute(
            "SELECT * FROM source WHERE provider = ? AND forked_from_id IN"
            f" ({_marks(len(frontier))})",
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


def _family(
    conn: sqlite3.Connection, source: sqlite3.Row
) -> list[sqlite3.Row]:
    """Return every other source in a fork family that could hold a copy.

    That is the ancestors, and the forks of the source and of each ancestor
    (so siblings too).  An unrelated session is never part of it.
    """
    chain = [source, *_ancestors(conn, source)]
    related: dict[int, sqlite3.Row] = {r["id"]: r for r in chain[1:]}
    for member in chain:
        related.update((r["id"], r) for r in _fork_sources(conn, member))
    related.pop(source["id"], None)
    return list(related.values())


def _add_line(
    conn: sqlite3.Connection,
    target: Target,
    source: sqlite3.Row,
    line: int,
    *,
    copies: bool = True,
) -> None:
    """Add every part of one line; a line tombstone blocks all its parts.

    Each event also gets a content tombstone, and with ``copies`` the same
    content already stored in the thread's fork family (its ancestors, its
    forks and their siblings) is added too, so an erased line cannot come
    back through another copy of the history.  A fork's own replayed prefix
    is not stored, so the ancestor's copy is the one that matters when the
    erase starts from a fork's ref.
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
    key = key_for(conn)
    tags = {event_tag(key, r[2], r[3]) for r in rows}
    target.tombstones += [
        tombstone_row(
            source["provider"],
            "line",
            thread_id=source["thread_id"],
            line=0,
            line_sha256=tag,
        )
        for tag in sorted(tags)
    ]
    if not copies:
        return
    for fork in _family(conn, source):
        for role, text in {(r[2], r[3]) for r in rows}:
            for (copy,) in conn.execute(
                "SELECT DISTINCT line FROM event WHERE source_id = ?"
                " AND role = ? AND text = ?",
                (fork["id"], role, text),
            ).fetchall():
                _add_line(conn, target, fork, copy, copies=False)


def _cites(
    conn: sqlite3.Connection, where: str, args: tuple[object, ...]
) -> set[int]:
    """Ids of live citations matching a fixed (never user-built) clause."""
    return {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM citation WHERE state = 'live' AND {where}", args
        )
    }


def collect_knowledge(conn: sqlite3.Connection, target: Target) -> None:
    """Extend the plan with citations and knowledge entries that follow.

    Citations of erased events are erased. An entry is erased when its text
    matched or when every one of its live citations is erased, because an
    entry nobody can verify any more is not worth keeping.

    Args:
        conn: Read connection.
        target: Plan to extend.
    """
    for source in target.sources.values():
        target.citations |= _cites(
            conn,
            "provider = ? AND thread_id = ?",
            (source["provider"], source["thread_id"]),
        )
    for source_id, line in target.lines:
        source = target.touched[source_id]
        target.citations |= _cites(
            conn,
            "provider = ? AND thread_id = ? AND line = ?",
            (source["provider"], source["thread_id"], line),
        )
    if not target.citations:
        return
    entries = conn.execute(
        "SELECT knowledge_id, sum(id NOT IN"
        f" ({_marks(len(target.citations))})) FROM citation"
        " WHERE state = 'live' GROUP BY knowledge_id",
        tuple(target.citations),
    ).fetchall()
    uncited = {kid for kid, live_left in entries if live_left == 0}
    target.knowledge |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM knowledge WHERE status != 'erased'"
            f" AND id IN ({_marks(len(uncited)) or 'NULL'})",
            tuple(uncited),
        )
    }
