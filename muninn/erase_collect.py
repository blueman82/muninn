"""Erase planning: find everything one erase request removes.

Nothing is written here. The collectors fill a ``Target`` from read-only
queries so ``--dry-run`` can report exact counts and ``erase`` can then apply
the whole plan in one transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from muninn import query
from muninn.erase_family import copies_of
from muninn.knowledge_erase import SupportClosure, support_closure
from muninn.tombstone_key import key_for
from muninn.tombstones import TombstoneRow, event_tag, tombstone_row

# Providers blocked when a session has not been ingested yet, so a later
# ingest cannot resurrect it.
_ALL_PROVIDERS = ["codex", "claude", "cursor"]


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
    # (provider, thread, role, text) of each content tombstone to write.
    # Kept as text, not tags, so planning (and a dry run) needs no key.
    content: list[tuple[str, str, str, str]] = field(
        default_factory=list[tuple[str, str, str, str]]
    )
    knowledge: set[str] = field(default_factory=set[str])
    review: SupportClosure = field(
        default_factory=lambda: SupportClosure(set(), set(), set(), set())
    )


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
    """Collect literal matches across transcript and governed review content.

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
    plan = target.review
    plan.evidence.update(
        r[0]
        for r in conn.execute(
            "SELECT id FROM review_evidence WHERE instr(excerpt,?)>0", (match,)
        )
    )
    plan.versions.update(
        (r[0], r[1])
        for r in conn.execute(
            "SELECT v.knowledge_id,v.revision FROM knowledge_version v "
            "JOIN knowledge k "
            "ON k.id=v.knowledge_id WHERE EXISTS(SELECT 1 FROM "
            "json_tree(v.content_json) j WHERE j.type='text' "
            "AND instr(j.atom,?)>0) OR "
            "instr(v.reason,?)>0 "
            "OR instr(k.retracted_reason,?)>0 OR EXISTS(SELECT 1 FROM "
            "json_tree(v.expiry_json) j WHERE j.type='text' "
            "AND instr(j.atom,?)>0) "
            "OR EXISTS (SELECT 1 FROM knowledge_claim c WHERE "
            "c.knowledge_id=v.knowledge_id AND c.revision=v.revision "
            "AND instr(c.text,?)>0)",
            (match, match, match, match, match),
        )
    )
    plan.requests.update(
        r[0]
        for r in conn.execute(
            "SELECT request_id FROM review_receipt WHERE "
            "instr(no_change_reason,?)>0",
            (match,),
        )
    )
    plan.unresolved.update(
        r[0]
        for r in conn.execute(
            "SELECT id FROM unresolved u WHERE EXISTS(SELECT 1 FROM "
            "json_tree(u.item_json) j WHERE j.type='text' "
            "AND instr(j.atom,?)>0)",
            (match,),
        )
    )
    plan = support_closure(
        conn,
        tuple(plan.evidence),
        request_ids=tuple(plan.requests),
        versions=tuple(plan.versions),
        unresolved_ids=tuple(plan.unresolved),
    )
    # Re-admitting the producing inputs must not regenerate matched output.
    for ident in plan.requests:
        plan.evidence.update(
            r[0]
            for r in conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=?",
                (ident,),
            )
        )
    target.review = plan


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
    content already stored as a copy of the history in the thread's fork
    family is added too (see ``erase_family.copies_of``), so an erased line
    cannot come back through another copy.

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
        _add_line(conn, target, member, copy, copies=False)


def content_tombstones(
    conn: sqlite3.Connection, target: Target
) -> list[TombstoneRow]:
    """Make the content tombstones of a plan, creating the key if needed.

    This is the only place a plan needs the tombstone key, so it runs when
    the plan is applied and a dry run leaves no key file behind.

    Args:
        conn: Connection to the store.
        target: The plan.

    Returns:
        One keyed content tombstone for each planned role and text.

    Raises:
        TombstoneKeyError: Propagated from ``key_for``.
    """
    if not target.content:
        return []
    key = key_for(conn)
    return [
        tombstone_row(
            provider,
            "line",
            thread_id=thread,
            line=0,
            line_sha256=event_tag(key, role, text),
        )
        for provider, thread, role, text in target.content
    ]


def collect_knowledge(conn: sqlite3.Connection, target: Target) -> None:
    """Extend transcript selection to every governed support association."""
    plan = target.review
    for provider, root in target.sessions:
        plan.evidence.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM review_evidence WHERE provider=? AND "
                "session_root=?",
                (provider, root),
            )
        )
        plan.requests.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM review_request WHERE provider=? AND "
                "session_root=?",
                (provider, root),
            )
        )
    seen: set[str] = set()
    while True:
        before = (len(target.lines), len(plan.evidence))
        for source_id, line in tuple(target.lines):
            source = target.touched[source_id]
            plan.evidence.update(
                r[0]
                for r in conn.execute(
                    "SELECT id FROM review_evidence WHERE provider=? AND "
                    "thread_id=? "
                    "AND (line=? OR line_sha256 IN (SELECT line_sha256 FROM "
                    "event WHERE source_id=? AND line=?))",
                    (
                        source["provider"],
                        source["thread_id"],
                        line,
                        source_id,
                        line,
                    ),
                )
            )
            plan.requests.update(
                r[0]
                for r in conn.execute(
                    "SELECT id FROM review_request WHERE provider=? AND "
                    "thread_id=? "
                    "AND from_line<=? AND through_line>=?",
                    (source["provider"], source["thread_id"], line, line),
                )
            )
        _evidence_lines(conn, target, plan.evidence - seen)
        seen.update(plan.evidence)
        if before == (len(target.lines), len(plan.evidence)):
            break
    target.review = support_closure(
        conn,
        tuple(plan.evidence),
        request_ids=tuple(plan.requests),
        versions=tuple(plan.versions),
        unresolved_ids=tuple(plan.unresolved),
    )
    target.knowledge = {ident for ident, _ in target.review.versions}


def _evidence_lines(
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
            _add_line(conn, target, source, line[0])
