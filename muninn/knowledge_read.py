"""Read side of the knowledge ledger.

Every reader works on a ``connect_ro`` connection and never writes. Entries
are rendered with the verification state of each citation so a reader can see
when the evidence behind a memory has changed or disappeared.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from muninn import query, scope
from muninn.knowledge_model import (
    BLOCK_QUOTE,
    CHAIN_MAX,
    KINDS,
    PROBLEMS_MAX,
    PUSHABLE_CITE,
    STATUSES,
    Entry,
    entry_name,
    iso_date,
    parse_kid,
    text_backed,
)
from muninn.query import NOTICE, guarded


def _ref(row: sqlite3.Row) -> str:
    """Return the ``provider:thread:line.part`` reference of a citation."""
    return f"{row['provider']}:{row['thread_id']}:{row['line']}.{row['part']}"


def _error(code: str) -> dict[str, Any]:
    """Return the standard error payload for a refused read."""
    return {"error": code, "notice": NOTICE}


@guarded
def verify_citation(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Re-check one citation against the live store.

    Args:
        conn: Open store connection.
        row: A ``citation`` table row.

    Returns:
        ``erased`` if the citation was scrubbed; ``changed`` if the cited
        line is now another line (its hash differs or the quote is gone) or
        no longer exists in a live source; ``missing`` if the provider
        deleted the source (the event is kept) or nothing is left to look
        at; ``ok`` if the event is there with the same hash and the quote is
        still in its text.
    """
    if row["state"] == "erased" or row["quote"] is None:
        return "erased"
    where = "s.provider = ? AND s.thread_id = ?"
    event = conn.execute(
        "SELECT e.id, e.line_sha256, s.status FROM event e"
        " JOIN source s ON s.id = e.source_id"
        f" WHERE {where} AND e.line = ? AND e.part = ?",
        (row["provider"], row["thread_id"], row["line"], row["part"]),
    ).fetchone()
    if event is None:
        source = conn.execute(
            f"SELECT s.status FROM source s WHERE {where}",
            (row["provider"], row["thread_id"]),
        ).fetchone()
        return (
            "changed" if source and source["status"] == "active" else "missing"
        )
    found: dict[str, Any] = query.quote_check(
        conn, str(event["id"]), row["quote"]
    )
    # The hash proves the line is the same one that was cited; the quote
    # check proves the words still occur in it.
    if event["line_sha256"] != row["line_sha256"] or not found["match"]:
        return "changed"
    return "missing" if event["status"] == "missing" else "ok"


def _cite_view(conn: sqlite3.Connection, cite: sqlite3.Row) -> Entry:
    """Return the public description of one citation row."""
    return {
        "ref": _ref(cite),
        "role": cite["role"],
        "kind": cite["kind"],
        "ts": cite["ts"],
        "quote": cite["quote"],
        "span": (
            None
            if cite["span_start"] is None
            else [cite["span_start"], cite["span_end"]]
        ),
        "state": cite["state"],
        "verify": verify_citation(conn, cite),
    }


def entry(conn: sqlite3.Connection, kid: int) -> Entry:
    """Render one entry with its citations and their verification state."""
    row = conn.execute(
        "SELECT k.*, sc.label FROM knowledge k"
        " JOIN scope sc ON sc.id = k.scope_id WHERE k.id = ?",
        (kid,),
    ).fetchone()
    cites = conn.execute(
        "SELECT * FROM citation WHERE knowledge_id = ? ORDER BY id", (kid,)
    )
    out: Entry = {
        "id": f"K{row['id']}",
        "kind": row["kind"],
        "text": row["text"],
        "status": row["status"],
        "scope": row["label"],
        "actor": row["actor"],
        "date": iso_date(row["created_at"]),
        "supersedes": entry_name(row["supersedes"]),
        "superseded_by": entry_name(row["superseded_by"]),
        "cites": [_cite_view(conn, c) for c in cites],
    }
    if row["status"] == "retracted":
        out["retract_reason"] = row["retract_reason"]
    return out


@guarded
def list_entries(
    conn: sqlite3.Connection,
    *,
    cwd: str,
    status: str = "current",
    kind: str | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """List entries newest first, each with its citations' verification.

    Args:
        conn: Open store connection.
        cwd: Working directory whose repo scope (plus global) is listed.
        status: One of ``STATUSES`` or ``"all"``.
        kind: Restrict to one of ``KINDS``.
        all_projects: Drop the scope filter.

    Returns:
        The entries and their count, or an error payload for a bad filter.
    """
    if status != "all" and status not in STATUSES:
        return _error("bad_status")
    if kind is not None and kind not in KINDS:
        return _error("bad_kind")
    where = ["1"]
    args: list[Any] = []
    if status != "all":
        where.append("k.status = ?")
        args.append(status)
    if kind is not None:
        where.append("k.kind = ?")
        args.append(kind)
    if not all_projects:
        # [0] matches no scope: an unknown cwd lists nothing, not everything.
        ids = scope.scope_ids_for_read(conn, cwd) or [0]
        where.append(f"k.scope_id IN ({','.join('?' * len(ids))})")
        args += ids
    rows = conn.execute(
        f"SELECT k.id FROM knowledge k WHERE {' AND '.join(where)}"
        " ORDER BY k.created_at DESC, k.id DESC",
        args,
    ).fetchall()
    entries = [entry(conn, row["id"]) for row in rows]
    return {"notice": NOTICE, "count": len(entries), "entries": entries}


def _chain(conn: sqlite3.Connection, kid: int, column: str) -> list[Entry]:
    """Return the entries on one side of ``kid``'s supersede chain.

    ``column`` is interpolated into SQL, so it must be one of the two fixed
    column names the callers pass. The walk is capped and cycle-safe because
    a damaged store must not hang a reader.
    """
    found: list[Entry] = []
    seen = {kid}
    row = conn.execute(f"SELECT {column} FROM knowledge WHERE id = ?", (kid,))
    nxt = row.fetchone()[0]
    while nxt is not None and nxt not in seen and len(found) < CHAIN_MAX:
        seen.add(nxt)
        row = conn.execute(
            f"SELECT *, {column} AS step FROM knowledge WHERE id = ?", (nxt,)
        ).fetchone()
        found.append(
            {
                "id": f"K{row['id']}",
                "kind": row["kind"],
                "text": row["text"],
                "status": row["status"],
                "actor": row["actor"],
                "date": iso_date(row["created_at"]),
            }
        )
        nxt = row["step"]
    return found


@guarded
def show(conn: sqlite3.Connection, kid: int) -> dict[str, Any]:
    """Return one entry with its supersede chain in both directions and log.

    Args:
        conn: Open store connection.
        kid: Entry id in any form ``parse_kid`` accepts.

    Returns:
        The entry, chain and log, or a ``not_found`` error payload.
    """
    number = parse_kid(kid)
    if (
        number is None
        or not conn.execute(
            "SELECT 1 FROM knowledge WHERE id = ?", (number,)
        ).fetchone()
    ):
        return _error("not_found")
    log = conn.execute(
        "SELECT action, actor, at FROM knowledge_log"
        " WHERE knowledge_id = ? ORDER BY id",
        (number,),
    )
    return {
        "notice": NOTICE,
        "entry": entry(conn, number),
        "chain": {
            "supersedes": _chain(conn, number, "supersedes"),
            "superseded_by": _chain(conn, number, "superseded_by"),
        },
        "log": [
            {
                "action": r["action"],
                "actor": r["actor"],
                "at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["at"])
                ),
            }
            for r in log
        ],
    }


@guarded
def check(conn: sqlite3.Connection) -> dict[str, Any]:
    """Re-verify every citation.

    Problems are named by entry and ref, never by text, so the report can be
    shown without leaking stored content.

    Args:
        conn: Open store connection.

    Returns:
        Counts of ok, changed, missing and erased citations, plus the
        changed or missing ones (capped at ``PROBLEMS_MAX``).
    """
    counts: dict[str, int] = dict.fromkeys(
        ("ok", "changed", "missing", "erased"), 0
    )
    problems: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT c.*, k.status AS entry_status FROM citation c"
        " JOIN knowledge k ON k.id = c.knowledge_id ORDER BY c.id"
    ).fetchall()
    for row in rows:
        state = verify_citation(conn, row)
        counts[state] += 1
        if state in ("changed", "missing"):
            problems.append(
                {
                    "id": f"K{row['knowledge_id']}",
                    "status": row["entry_status"],
                    "ref": _ref(row),
                    "state": state,
                }
            )
    out: dict[str, Any] = {
        "notice": NOTICE,
        "citations": len(rows),
        **counts,
    }
    out["problems"] = problems[:PROBLEMS_MAX]
    if len(problems) > PROBLEMS_MAX:
        out["problems_omitted"] = len(problems) - PROBLEMS_MAX
    return out


# Citation rows read per wanted entry, and the most ever read, so the hook's
# cost stays flat as the ledger grows; Python then drops entries whose quote
# does not back the text.  Beyond the cap an older entry is simply not pushed.
_OVERFETCH = 10
_SCAN_CAP = 200


@guarded
def block_entries(
    conn: sqlite3.Connection, scope_ids: list[int], limit: int = 8
) -> list[dict[str, Any]]:
    """Return the entries the SessionStart block may push.

    Only current entries with a live user-prompt citation whose quote backs
    the entry text qualify (``text_backed``); entries cited by replies or
    tool calls alone, or whose text the user's words do not back, stay
    pull-only.

    Args:
        conn: Open store connection.
        scope_ids: Scopes to draw from.
        limit: Maximum number of entries.

    Returns:
        Newest first, each with its actor and the first backing user-prompt
        quote cut to ``BLOCK_QUOTE`` characters.
    """
    if not scope_ids or limit < 1:
        return []
    cursor = conn.execute(
        "SELECT k.id, k.kind, k.text, k.actor, k.created_at, sc.label,"
        " m.provider, m.thread_id, m.line, m.part, m.quote"
        " FROM knowledge k JOIN scope sc ON sc.id = k.scope_id"
        " JOIN citation m ON m.knowledge_id = k.id"
        f" AND {PUSHABLE_CITE}"
        f" WHERE k.status = 'current' AND k.scope_id IN"
        f" ({','.join('?' * len(scope_ids))})"
        " ORDER BY k.created_at DESC, k.id DESC, m.id LIMIT ?",
        [*scope_ids, min(limit * _OVERFETCH, _SCAN_CAP)],
    )
    seen: set[int] = set()
    rows: list[sqlite3.Row] = []
    for r in cursor:
        if r["id"] in seen or not text_backed(r["text"], r["quote"] or ""):
            continue
        seen.add(r["id"])
        rows.append(r)
        if len(rows) == limit:
            break
    return [
        {
            "id": f"K{r['id']}",
            "kind": r["kind"],
            "scope": r["label"],
            "text": r["text"],
            "actor": r["actor"],
            "date": iso_date(r["created_at"]),
            "cite": _ref(r),
            "quote": r["quote"][:BLOCK_QUOTE],
        }
        for r in rows
    ]


@guarded
def user_cited(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """Return which entry ids may be pushed into a prompt.

    This is the same rule ``block_entries`` applies: a live user-prompt
    citation whose quote backs the entry text.

    Args:
        conn: Open store connection.
        ids: Candidate entry numbers.

    Returns:
        The subset of ``ids`` that qualify.
    """
    rows = conn.execute(
        "SELECT k.id, k.text, m.quote FROM knowledge k"
        " JOIN citation m ON m.knowledge_id = k.id"
        f" WHERE k.id IN ({','.join('?' * len(ids))})"
        f" AND {PUSHABLE_CITE}",
        ids,
    )
    return {r[0] for r in rows if text_backed(r[1], r[2] or "")}
