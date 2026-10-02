"""Candidate retrieval, de-duplication and rendering of search hits."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from typing import Any

from muninn.query.answers import Answer, answer_citable, event_ref

CANDIDATES = 300  # bm25 candidates per search, composed into pages
SNIPPET_TOKENS = 32
PER_SESSION = 2  # hits per session on a page
TOOLS_PER_PAGE = 4
KNOWLEDGE_HITS = 3
KNOWLEDGE_TEXT = 300  # chars of a knowledge entry shown before size trimming

# One composed hit: the event row plus how many identical repeats it absorbed.
type Entry = dict[str, Any]

_HITS_FROM = """
FROM event_fts
JOIN event e ON e.id = event_fts.rowid
JOIN source s ON s.id = e.source_id
JOIN scope sc ON sc.id = e.scope_id
WHERE event_fts MATCH ? AND """
CANDIDATES_SQL = (
    "SELECT e.id, e.line, e.part, e.ts, e.role, e.kind, e.tag, e.cwd, e.text,"
    " e.flags, s.provider, s.thread_id, s.session_root, s.status,"
    " s.thread_class, sc.label"
    + _HITS_FROM
    + "{where} ORDER BY bm25(event_fts), e.id"
    " LIMIT {limit}"
)
# Every eligible match, in scope or not, per label and session. `inside` is
# the scope predicate evaluated as a column so one pass yields both counts.
_TALLY_SQL = (
    "SELECT COALESCE(({inside}), 0) AS inside, sc.label, s.session_root,"
    " count(*) AS n" + _HITS_FROM + "{where} GROUP BY inside, sc.label,"
    " s.session_root"
)


def tally(
    conn: sqlite3.Connection,
    fts: str,
    where: str,
    params: list[Any],
    inside: tuple[str, list[Any]],
) -> tuple[Counter[str], Counter[str]]:
    """Count every eligible match, not just the top candidates.

    Args:
        conn: Read-only store connection.
        fts: The FTS5 MATCH string.
        where: Eligibility predicate shared with the candidate query.
        params: Parameters of ``where``.
        inside: The scope predicate and its parameters.

    Returns:
        Matches per session inside the scope, and per scope label outside it.
    """
    rows = conn.execute(
        _TALLY_SQL.format(inside=inside[0], where=where),
        [*inside[1], fts, *params],
    )
    total: Counter[str] = Counter()
    other: Counter[str] = Counter()
    for row in rows:
        if row["inside"]:
            total[row["session_root"]] += row["n"]
        else:
            other[row["label"]] += row["n"]
    return total, other


def compose(
    rows: list[sqlite3.Row],
    limit: int,
    *,
    per_session: bool,
    tools: bool,
) -> tuple[list[Entry], int, int]:
    """Turn rank-ordered rows into page-ready entries.

    Identical text within a session collapses into its first hit (repeats);
    then at most PER_SESSION hits per session and TOOLS_PER_PAGE tool_call
    hits in each ``limit``-sized page are kept.

    Args:
        rows: Candidate rows in rank order.
        limit: Hits per page, which sets where the tool budget resets.
        per_session: Cap the hits any one session may contribute.
        tools: Cap the tool_call hits on each page.

    Returns:
        The entries, how many the session cap dropped and how many the tool
        cap dropped.
    """
    entries: list[Entry] = []
    seen: dict[tuple[str, int], Entry] = {}
    shown: Counter[str] = Counter()
    session_capped = tool_capped = in_page_tools = 0
    for row in rows:
        root = row["session_root"]
        # hash() is salted per process, which is fine: the key never leaves
        # this call, and it avoids holding every text twice.
        key = (root, hash(row["text"]))
        if key in seen:
            seen[key]["repeats"] += 1
            continue
        if per_session and shown[root] >= PER_SESSION:
            session_capped += 1
            continue
        is_tool = row["kind"] == "tool_call"
        if tools and is_tool and in_page_tools >= TOOLS_PER_PAGE:
            tool_capped += 1
            continue
        entry = seen[key] = {"row": row, "repeats": 0}
        entries.append(entry)
        shown[root] += 1
        in_page_tools += is_tool
        if len(entries) % limit == 0:
            in_page_tools = 0
    return entries, session_capped, tool_capped


def _snippets(
    conn: sqlite3.Connection, fts: str, ids: list[int]
) -> dict[int, str]:
    """Return highlighted snippets keyed by event id."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT rowid, snippet(event_fts, 0, '«', '»', '…', {SNIPPET_TOKENS})"
        f" FROM event_fts WHERE event_fts MATCH ? AND rowid IN ({marks})",
        [fts, *ids],
    )
    return {row[0]: row[1] for row in rows}


def _hit(row: sqlite3.Row, snippet: str, repeats: int) -> Answer:
    """Return one rendered hit."""
    hit: Answer = {
        "id": row["id"],
        "ref": event_ref(row),
        "ts": row["ts"],
        "provider": row["provider"],
        "session": row["session_root"][:8],
        "thread": row["thread_id"][:8],
        "kind": row["kind"],
        "role": row["role"],
        "tag": row["tag"],
        "scope": row["label"],
        "cwd": row["cwd"],
        "source_status": row["status"],
        "snippet": snippet,
        "answer_citable": answer_citable(row),
        "repeats": repeats,
    }
    if row["thread_class"] != "primary":
        hit["class"] = row["thread_class"]
    return hit


def render(
    conn: sqlite3.Connection,
    fts: str,
    shown: list[Entry],
    entries: list[Entry],
    total: Counter[str],
) -> list[Answer]:
    """Render the shown entries as hits.

    A session with at least PER_SESSION hits reports how many of its
    in-scope matches no hit stands for (``more_in_session``).

    Args:
        conn: Read-only store connection.
        fts: The FTS5 MATCH string, reused to build snippets.
        shown: Entries to render, in display order.
        entries: Every composed entry, which defines what hits cover.
        total: In-scope match counts per session.

    Returns:
        One hit per shown entry.
    """
    hits_in: Counter[str] = Counter()
    covered: Counter[str] = Counter()
    for entry in entries:
        root = entry["row"]["session_root"]
        hits_in[root] += 1
        covered[root] += 1 + entry["repeats"]
    snippets = _snippets(conn, fts, [e["row"]["id"] for e in shown])
    hits: list[Answer] = []
    for entry in shown:
        row = entry["row"]
        hit = _hit(row, snippets.get(row["id"], ""), entry["repeats"])
        root = row["session_root"]
        more = total[root] - covered[root]
        if hits_in[root] >= PER_SESSION and more > 0:
            hit["more_in_session"] = more
        hits.append(hit)
    return hits


def _cites(conn: sqlite3.Connection, knowledge_id: int) -> list[str]:
    """Return up to two live citations of a knowledge entry."""
    rows = conn.execute(
        "SELECT provider, thread_id, line, part FROM citation"
        " WHERE knowledge_id = ? AND state = 'live' ORDER BY id LIMIT 2",
        (knowledge_id,),
    )
    return [event_ref(row) for row in rows]


def knowledge(
    conn: sqlite3.Connection, fts: str, ids: list[int], everywhere: bool
) -> list[Answer]:
    """Return the top current knowledge entries in scope, with citations."""
    if not everywhere and not ids:
        return []
    where = (
        "" if everywhere else f"AND k.scope_id IN ({','.join('?' * len(ids))})"
    )
    rows = conn.execute(
        "SELECT k.id, k.kind, k.text, k.actor, k.created_at, sc.label"
        " FROM knowledge_fts JOIN knowledge k ON k.id = knowledge_fts.rowid"
        " JOIN scope sc ON sc.id = k.scope_id"
        f" WHERE knowledge_fts MATCH ? AND k.status = 'current' {where}"
        f" ORDER BY bm25(knowledge_fts), k.id LIMIT {KNOWLEDGE_HITS}",
        [fts, *([] if everywhere else ids)],
    ).fetchall()
    return [
        {
            "id": f"K{row['id']}",
            "kind": row["kind"],
            "text": row["text"][:KNOWLEDGE_TEXT],
            "scope": row["label"],
            "actor": row["actor"],
            "date": time.strftime("%Y-%m-%d", time.gmtime(row["created_at"])),
            "cites": _cites(conn, row["id"]),
        }
        for row in rows
    ]
