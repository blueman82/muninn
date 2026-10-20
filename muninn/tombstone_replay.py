"""Plan persisted transcript erasure before any recorded history is deleted."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from muninn.erase_family import replayed_run
from muninn.tombstone_intent import IntentRow, TombstoneIntent, event_tag


@dataclass(frozen=True)
class TranscriptPlan:
    """Identify exact transcript rows in one immutable pre-deletion plan."""

    sources: frozenset[int]
    events: frozenset[int]


def plan_transcripts(
    conn: sqlite3.Connection, intent: TombstoneIntent
) -> TranscriptPlan:
    """Select recorded providers, identities and exact physical line hashes."""
    sources: set[int] = set()
    events: set[int] = set()
    for row in intent.rows:
        if row.level in ("session", "thread"):
            column = "session_root" if row.level == "session" else "thread_id"
            value = (
                row.session_root if row.level == "session" else row.thread_id
            )
            sources.update(
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM source WHERE provider=? AND {column}=?",
                    (row.provider, value),
                )
            )
        elif row.line:
            events.update(
                r[0]
                for r in conn.execute(
                    "SELECT e.id FROM event e JOIN source s "
                    "ON s.id=e.source_id "
                    "WHERE s.provider=? AND s.thread_id=? AND e.line=? "
                    "AND e.line_sha256=?",
                    (row.provider, row.thread_id, row.line, row.line_sha256),
                )
            )
    for ident in sources:
        events.update(
            r[0]
            for r in conn.execute(
                "SELECT id FROM event WHERE source_id=?", (ident,)
            )
        )
    plan = TranscriptPlan(frozenset(sources), frozenset(events))
    _validate_family(conn, intent, plan)
    return plan


def _ancestors(
    source: sqlite3.Row, sources: dict[tuple[str, str], sqlite3.Row]
) -> tuple[frozenset[str], bool]:
    """Retain missing identities instead of assuming empty history."""
    found: set[str] = set()
    parent = source["forked_from_id"]
    while parent and parent not in found and parent != source["thread_id"]:
        found.add(parent)
        ancestor = sources.get((source["provider"], parent))
        if ancestor is None:
            return frozenset(found), False
        parent = ancestor["forked_from_id"]
    return frozenset(found), parent is None


def _matching(
    event: sqlite3.Row, rows: tuple[IntentRow, ...], key: bytes | None
) -> bool:
    """Compare supplemental evidence only inside an already related family."""
    digests = {r.line_sha256 for r in rows}
    return event["line_sha256"] in digests or (
        key is not None
        and event_tag(key, event["role"], event["text"]) in digests
    )


def _family_lines(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    rows: tuple[IntentRow, ...],
    intent: TombstoneIntent,
    plan: TranscriptPlan,
    *,
    complete: bool,
    ancestor: bool = False,
) -> None:
    """Refuse uncovered copies without broadening a physical selection."""
    tags = frozenset(
        r.line_sha256 for r in rows if r.line == 0 and r.line_sha256
    )
    hashes = frozenset(r.line_sha256 for r in rows if r.line and r.line_sha256)
    prefix = {
        line
        for line, _, _ in replayed_run(
            conn, source, tags=tags, hashes=hashes, key=intent.content_key
        )
    }
    cursor = conn.execute(
        "SELECT * FROM event WHERE source_id=?", (source["id"],)
    )
    cursor.row_factory = sqlite3.Row
    for event in cursor:
        if event["id"] in plan.events or not _matching(
            event, rows, intent.content_key
        ):
            continue
        # A different current hash at an explicitly recorded coordinate is a
        # replacement, not authority to erase new content by a matching tag.
        if any(
            r.provider == source["provider"]
            and r.thread_id == source["thread_id"]
            and r.line == event["line"]
            and r.line_sha256 != event["line_sha256"]
            for r in intent.rows
        ):
            continue
        if ancestor or not complete or event["line"] in prefix:
            raise ValueError("erasure_history_unverified")


def _validate_own_tags(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    rows: tuple[IntentRow, ...],
    intent: TombstoneIntent,
    plan: TranscriptPlan,
    *,
    complete: bool,
) -> None:
    """Check unexplained new tags and tags with complete physical bindings."""
    physical = tuple(row for row in rows if row.line)
    covered: set[str] = set()
    bound = True
    for row in physical:
        events = conn.execute(
            "SELECT role,text,line_sha256 FROM event "
            "WHERE source_id=? AND line=?",
            (source["id"], row.line),
        ).fetchall()
        if not any(event[2] == row.line_sha256 for event in events):
            bound = False
        if intent.content_key is not None:
            # Changed coordinates explain surviving own content without
            # becoming deletion targets.
            covered.update(
                event_tag(intent.content_key, event[0], event[1])
                for event in events
            )
    # Scrubbed or replaced lines no longer bind old hashes to old tags.
    # A tag missing from the table cannot borrow that old erasure history.
    missing = {row.key for row in intent.missing}
    uncovered = tuple(
        row
        for row in rows
        if not row.line
        and row.line_sha256 not in covered
        and (bound or row.key in missing)
    )
    if uncovered:
        _family_lines(
            conn,
            source,
            uncovered,
            intent,
            plan,
            complete=complete,
            ancestor=True,
        )


def _validate_family(
    conn: sqlite3.Connection, intent: TombstoneIntent, plan: TranscriptPlan
) -> None:
    """Require persisted coverage before deletion can remove its evidence."""
    cursor = conn.execute("SELECT * FROM source")
    cursor.row_factory = sqlite3.Row
    sources = {(s["provider"], s["thread_id"]): s for s in cursor}
    lineages = {
        key: _ancestors(source, sources) for key, source in sources.items()
    }
    for source in sources.values():
        if source["id"] in plan.sources:
            continue
        ancestors, complete = lineages[source["provider"], source["thread_id"]]
        relevant = tuple(
            r for r in intent.rows if r.provider == source["provider"]
        )
        own_rows = tuple(
            r
            for r in relevant
            if r.level == "line" and r.thread_id == source["thread_id"]
        )
        _validate_own_tags(
            conn, source, own_rows, intent, plan, complete=complete
        )
        roots = {
            sources[source["provider"], a]["session_root"]
            for a in ancestors
            if (source["provider"], a) in sources
        } | set(ancestors)
        if any(
            (r.level == "session" and r.session_root in roots)
            or (
                r.level == "thread"
                and r.thread_id == source["parent_thread_id"]
            )
            for r in relevant
        ):
            raise ValueError("erasure_history_unverified")
        descendants = {
            thread
            for (provider, thread), (parents, _) in lineages.items()
            if provider == source["provider"]
            and source["thread_id"] in parents
        }
        older_rows = tuple(
            r
            for r in relevant
            if r.level == "line" and r.thread_id in descendants
        )
        if older_rows:
            _family_lines(
                conn,
                source,
                older_rows,
                intent,
                plan,
                complete=complete,
                ancestor=True,
            )
        relatives = ancestors | {
            thread
            for (provider, thread), (parents, _) in lineages.items()
            if provider == source["provider"] and parents & ancestors
        }
        rows = tuple(
            r
            for r in relevant
            if r.level == "line"
            and r.thread_id in relatives
            and r.thread_id != source["thread_id"]
        )
        if rows:
            _family_lines(conn, source, rows, intent, plan, complete=complete)
