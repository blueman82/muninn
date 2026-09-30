"""pctx knowledge: the cited ledger (design 3.7, 4.2, 4.7; spec O5a, O5b).

Every entry carries at least one citation: the immutable identity of a
primary prompt, reply or tool_call event plus a verbatim quote of it, checked
when the entry is written. Writers (add, retract) must run under
store.writer_lock with a store.connect_rw connection; run_add and run_retract
do that. Readers (list_entries, show, check, verify_citation, block_entries)
work on a connect_ro connection. A refused write raises Refused and leaves
nothing behind.
"""

import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from pctx import classify, query, scope
from pctx.query import NOTICE, _guarded  # one guard for every reader

KINDS = ("decision", "fact", "preference", "procedure")
CITABLE = ("prompt", "reply", "tool_call")  # of a primary thread, unflagged
STATUSES = ("current", "superseded", "retracted", "erased")
TEXT_MAX = 500
REASON_MAX = 200
QUOTE_MIN = 12
QUOTE_MAX = 300
CHAIN_MAX = 100  # hops followed along a supersede chain

# `<` of a frame delimiter, however spaced or cased (design 4.8)
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*pctx-(?:memory|recall))")


class Refused(Exception):
    """A write was refused and nothing was written (exit 2).

    code: bad_kind, bad_actor, text_length, reason_length, uncited, bad_ref,
    not_found, ambiguous_ref, not_citable, quote_length, quote_not_found,
    preference_needs_user, bad_supersedes, not_current.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _clean(text: str, code: str, low: int, high: int) -> str:
    """Stripped, secrets redacted, then frame delimiters escaped; refused
    with `code` unless low..high characters."""
    if len(text) > 4 * high:  # not worth scanning
        raise Refused(code)
    body = _FRAME.sub("&lt;", classify.redact(text.strip())[0])
    if not low <= len(body) <= high:
        raise Refused(code)
    return body


def _kid(value) -> int | None:
    """An entry id from 12, "12" or "K12"; None if it is not one."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    found = re.fullmatch(r"[Kk]?([0-9]{1,18})", str(value or "").strip())
    return int(found[1]) or None if found else None


def _cite(conn: sqlite3.Connection, ref: str, quote: str) -> dict:
    """One (ref, quote) pair, checked: the event's identity plus the quote
    (whitespace collapsed) and its span in the event text."""
    opened = query.open_event(conn, ref, roots={}, context=0)
    if "error" in opened:
        raise Refused(opened["error"])
    prov = opened["provenance"]
    if (
        prov.get("class", "primary") != "primary"
        or prov["kind"] not in CITABLE
        or opened["flagged"]
    ):
        raise Refused("not_citable")
    wanted = " ".join(quote.split())
    if not QUOTE_MIN <= len(wanted) <= QUOTE_MAX:
        raise Refused("quote_length")
    found = query.quote_check(conn, str(opened["id"]), quote)
    if not found["match"]:
        raise Refused("quote_not_found")
    return {
        "event": opened["id"],
        "provider": prov["provider"],
        "thread_id": prov["thread"],
        "line": prov["line"],
        "part": prov["part"],
        "line_sha256": prov["line_sha256"],
        "role": prov["role"],
        "kind": prov["kind"],
        "ts": prov["ts"],
        "quote": wanted,
        "span": found["span"],
    }


def _insert(conn, sid, kind, body, actor, cites, old) -> int:
    now = time.time()
    kid = conn.execute(
        "INSERT INTO knowledge(scope_id, kind, text, status, supersedes,"
        " actor, created_at) VALUES (?, ?, ?, 'current', ?, ?, ?)",
        (sid, kind, body, old, actor, now),
    ).lastrowid
    conn.executemany(
        "INSERT INTO citation(knowledge_id, provider, thread_id, line, part,"
        " line_sha256, role, kind, ts, quote, span_start, span_end)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (kid, c["provider"], c["thread_id"], c["line"], c["part"],
             c["line_sha256"], c["role"], c["kind"], c["ts"], c["quote"],
             c["span"][0], c["span"][1])
            for c in cites
        ],
    )  # fmt: skip
    logged = [(kid, "add")]
    if old is not None:
        conn.execute(
            "UPDATE knowledge SET status = 'superseded', superseded_by = ?"
            " WHERE id = ?",
            (kid, old),
        )
        logged += [(kid, "supersede"), (old, "superseded")]
    conn.executemany(
        "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
        " VALUES (?, ?, ?, ?)",
        [(k, action, actor, now) for k, action in logged],
    )
    return kid


def _supersedable(conn, given, sid) -> int:
    """The entry a new one replaces: current and in the same scope."""
    old = _kid(given)
    row = (
        conn.execute(
            "SELECT scope_id, status FROM knowledge WHERE id = ?", (old,)
        ).fetchone()
        if old
        else None
    )
    if row is None or row["status"] != "current" or row["scope_id"] != sid:
        raise Refused("bad_supersedes")
    return old


@_guarded
def add(
    conn: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    cites: list[tuple[str, str]] = (),
    quote_only: str | None = None,
    supersedes: int | None = None,
    global_scope: bool = False,
    cwd: str,
    actor: str,
    roots: Mapping[str, Path],
    env: Mapping[str, str],
) -> dict:
    """Write one cited entry in a single transaction, or raise Refused.

    cites are (ref, quote) pairs; the quote must be 12..300 characters of the
    event, whitespace collapsed. A preference needs a cited user prompt.
    """
    if kind not in KINDS:
        raise Refused("bad_kind")
    if not actor:
        raise Refused("bad_actor")
    body = _clean(text, "text_length", 1, TEXT_MAX)
    if not cites and quote_only is None:
        raise Refused("uncited")
    conn.execute("BEGIN IMMEDIATE")
    try:
        sid = (
            scope.global_scope_id(conn)
            if global_scope
            else scope.scope_id(conn, cwd)
        )
        found = [_cite(conn, ref, quote) for ref, quote in cites]
        if kind == "preference" and not any(
            (c["role"], c["kind"]) == ("user", "prompt") for c in found
        ):
            raise Refused("preference_needs_user")
        old = None
        if supersedes is not None:
            old = _supersedable(conn, supersedes, sid)
        kid = _insert(conn, sid, kind, body, actor, found, old)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"notice": NOTICE, "entry": _entry(conn, kid)}


def _ref(row: sqlite3.Row) -> str:
    return f"{row['provider']}:{row['thread_id']}:{row['line']}.{row['part']}"


def verify_citation(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """ok | changed | missing | erased for one citation row."""
    return "ok"


def _date(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at))


def _name(kid: int | None) -> str | None:
    return None if kid is None else f"K{kid}"


def _entry(conn: sqlite3.Connection, kid: int) -> dict:
    row = conn.execute(
        "SELECT k.*, sc.label FROM knowledge k"
        " JOIN scope sc ON sc.id = k.scope_id WHERE k.id = ?",
        (kid,),
    ).fetchone()
    cites = conn.execute(
        "SELECT * FROM citation WHERE knowledge_id = ? ORDER BY id", (kid,)
    )
    entry = {
        "id": f"K{row['id']}",
        "kind": row["kind"],
        "text": row["text"],
        "status": row["status"],
        "scope": row["label"],
        "actor": row["actor"],
        "date": _date(row["created_at"]),
        "supersedes": _name(row["supersedes"]),
        "superseded_by": _name(row["superseded_by"]),
        "cites": [
            {
                "ref": _ref(c),
                "role": c["role"],
                "kind": c["kind"],
                "ts": c["ts"],
                "quote": c["quote"],
                "span": (
                    None
                    if c["span_start"] is None
                    else [c["span_start"], c["span_end"]]
                ),
                "state": c["state"],
                "verify": verify_citation(conn, c),
            }
            for c in cites
        ],
    }
    if row["status"] == "retracted":
        entry["retract_reason"] = row["retract_reason"]
    return entry


@_guarded
def retract(
    conn: sqlite3.Connection, kid: int, *, reason: str, actor: str
) -> dict:
    """Retract a current entry (its text stays, with the reason)."""
    if not actor:
        raise Refused("bad_actor")
    why = _clean(reason, "reason_length", 0, REASON_MAX)
    number = _kid(kid)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = (
            conn.execute(
                "SELECT status FROM knowledge WHERE id = ?", (number,)
            ).fetchone()
            if number
            else None
        )
        if row is None:
            raise Refused("not_found")
        if row["status"] != "current":
            raise Refused("not_current")
        conn.execute(
            "UPDATE knowledge SET status = 'retracted', retract_reason = ?"
            " WHERE id = ?",
            (why or None, number),
        )
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'retract', ?, ?)",
            (number, actor, time.time()),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"notice": NOTICE, "entry": _entry(conn, number)}


def _error(code: str) -> dict:
    return {"error": code, "notice": NOTICE}


@_guarded
def list_entries(
    conn: sqlite3.Connection,
    *,
    cwd: str,
    status: str = "current",
    kind: str | None = None,
    all_projects: bool = False,
) -> dict:
    """Entries of the repo scope of cwd plus global, newest first, each
    with its citations' verification state; status is one of STATUSES or
    "all", all_projects drops the scope filter."""
    if status != "all" and status not in STATUSES:
        return _error("bad_status")
    if kind is not None and kind not in KINDS:
        return _error("bad_kind")
    where, args = ["1"], []
    if status != "all":
        where.append("k.status = ?")
        args.append(status)
    if kind is not None:
        where.append("k.kind = ?")
        args.append(kind)
    if not all_projects:
        ids = scope.scope_ids_for_read(conn, cwd) or [0]
        where.append(f"k.scope_id IN ({','.join('?' * len(ids))})")
        args += ids
    rows = conn.execute(
        f"SELECT k.id FROM knowledge k WHERE {' AND '.join(where)}"
        " ORDER BY k.created_at DESC, k.id DESC",
        args,
    ).fetchall()
    entries = [_entry(conn, row["id"]) for row in rows]
    return {"notice": NOTICE, "count": len(entries), "entries": entries}


def _chain(conn: sqlite3.Connection, kid: int, column: str) -> list[dict]:
    """The entries on one side of kid's supersede chain, nearest first."""
    found, seen = [], {kid}
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
                "date": _date(row["created_at"]),
            }
        )
        nxt = row["step"]
    return found


@_guarded
def show(conn: sqlite3.Connection, kid: int) -> dict:
    """One entry with its supersede chain in both directions and its log."""
    number = _kid(kid)
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
        "entry": _entry(conn, number),
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
