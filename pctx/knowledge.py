"""pctx knowledge: the cited ledger (design 3.7, 4.2, 4.7; spec O5a, O5b).

Every entry carries at least one citation: the immutable identity of a
primary prompt, reply or tool_call event plus a verbatim quote of it, checked
when the entry is written. Writers (add, retract) must run under
store.writer_lock with a store.connect_rw connection; run_add and run_retract
do that. Readers (list_entries, show, check, verify_citation, block_entries,
user_cited) work on a connect_ro connection. A refused write raises RefusedError
and leaves nothing behind.
"""

import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from pctx import classify, ingest, query, scope, store
from pctx.query import NOTICE, _guarded  # one guard for every reader

KINDS = ("decision", "fact", "preference", "procedure")
CITABLE = ("prompt", "reply", "tool_call")  # of a primary thread, unflagged
STATUSES = ("current", "superseded", "retracted", "erased")
TEXT_MAX = 500
REASON_MAX = 200
QUOTE_MIN = 12
QUOTE_MAX = 300
CALLER_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")
CHAIN_MAX = 100  # hops followed along a supersede chain
PROBLEMS_MAX = 100  # broken citations named by check()
BLOCK_QUOTE = 120  # characters of a quote pushed in the SessionStart block
# The citation rows (alias m) that let an entry be pushed into a prompt or a
# session (O5b): live, and of a user prompt. Anything else stays pull-only.
_PUSHABLE_CITE = "m.state = 'live' AND m.role = 'user' AND m.kind = 'prompt'"

# `<` of a frame delimiter, however spaced or cased (design 4.8)
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*pctx-(?:memory|recall))")


class RefusedError(Exception):
    """A write was refused and nothing was written (exit 2).

    code: bad_kind, bad_actor, text_length, reason_length, uncited, bad_ref,
    not_found, ambiguous_ref, not_citable, quote_length, quote_not_found,
    approval_needs_reply, no_caller_session, preference_needs_user,
    bad_supersedes, not_current.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _clean(text: str, code: str, low: int, high: int) -> str:
    """Stripped, secrets redacted, then frame delimiters escaped; refused
    with `code` unless low..high characters."""
    if len(text) > 4 * high:  # not worth scanning
        raise RefusedError(code)
    body = _FRAME.sub("&lt;", classify.redact(text.strip())[0])
    if not low <= len(body) <= high:
        raise RefusedError(code)
    return body


def _kid(value) -> int | None:
    """An entry id from 12, "12" or "K12"; None if it is not one."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    found = re.fullmatch(r"[Kk]?([0-9]{1,18})", str(value or "").strip())
    return int(found[1]) or None if found else None


def _cite(conn: sqlite3.Connection, ref: str, quote: str) -> dict:
    """One (ref, quote) pair, checked: the event's identity plus the quote
    (whitespace collapsed) and its span in the event text.

    A quote under QUOTE_MIN characters is only accepted as the whole text of
    a user prompt: an approval ("approval": True), valid once the entry
    also cites the reply it answers (_proposals, spec O5a).
    """
    opened = query.open_event(conn, ref, roots={}, context=0)
    if "error" in opened:
        raise RefusedError(opened["error"])
    prov = opened["provenance"]
    if (
        prov.get("class", "primary") != "primary"
        or prov["kind"] not in CITABLE
        or opened["flagged"]
    ):
        raise RefusedError("not_citable")
    wanted = " ".join(quote.split())
    if not wanted or len(wanted) > QUOTE_MAX:
        raise RefusedError("quote_length")
    found = query.quote_check(conn, str(opened["id"]), quote)
    if not found["match"]:
        raise RefusedError("quote_not_found")
    short = len(wanted) < QUOTE_MIN
    if short and not (
        (prov["role"], prov["kind"]) == ("user", "prompt")
        and " ".join(opened["text"].split()) == wanted
    ):
        raise RefusedError("quote_length")
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
        "approval": short,
    }


def _proposals(conn: sqlite3.Connection, cites: list[dict]) -> None:
    """Each approval needs the reply right before it, in its own thread,
    among the entry's citations (O5a: proposal and approval)."""
    cited = {c["event"] for c in cites}
    for cite in cites:
        if not cite["approval"]:
            continue
        before = conn.execute(
            "SELECT p.id FROM event p"
            " JOIN event a ON a.source_id = p.source_id"
            " WHERE a.id = ? AND p.kind = 'reply'"
            " AND (p.line, p.part) < (a.line, a.part)"
            " ORDER BY p.line DESC, p.part DESC LIMIT 1",
            (cite["event"],),
        ).fetchone()
        if before is None or before["id"] not in cited:
            raise RefusedError("approval_needs_reply")


def _caller(conn, roots, env) -> str | None:
    """The caller's session root, after a targeted ingest of its own
    threads so the prompt just typed is there. It runs on this connection:
    the caller of add holds the writer lock, which is not reentrant."""
    ids = {env.get(name) for name in CALLER_ENV} - {None, ""}
    if ids and roots:
        # never None in the set: ingest matches a continuation file's None
        ingest.ingest(
            conn, roots, only_threads=ids | {query.caller_root(conn, env)}
        )
    return query.caller_root(conn, env)


def _caller_prompt(conn: sqlite3.Connection, root: str, quote: str) -> dict:
    """The caller's latest citable user prompt that holds the quote."""
    wanted = " ".join(quote.split())
    if not wanted or len(wanted) > QUOTE_MAX:
        raise RefusedError("quote_length")
    rows = conn.execute(
        "SELECT e.id FROM event e JOIN source s ON s.id = e.source_id"
        " WHERE s.session_root = ? AND e.kind = 'prompt' AND e.role = 'user'"
        " AND instr(e.text, ?) > 0"
        " ORDER BY COALESCE(e.ts, '') DESC, s.thread_id DESC,"
        " e.line DESC, e.part DESC",
        (root, wanted.split(" ", 1)[0]),
    ).fetchall()
    for row in rows:
        try:
            return _cite(conn, str(row["id"]), quote)
        except RefusedError:  # not this prompt: not citable, or another wording
            continue
    raise RefusedError("quote_not_found")


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
        raise RefusedError("bad_supersedes")
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
    """Write one cited entry in a single transaction, or raise RefusedError.

    cites are (ref, quote) pairs; the quote must be 12..300 characters of the
    event, whitespace collapsed. A preference needs a cited user prompt.
    """
    if kind not in KINDS:
        raise RefusedError("bad_kind")
    if not actor:
        raise RefusedError("bad_actor")
    body = _clean(text, "text_length", 1, TEXT_MAX)
    if not cites and quote_only is None:
        raise RefusedError("uncited")
    root = _caller(conn, roots, env)
    if quote_only is not None and root is None:
        raise RefusedError("no_caller_session")
    conn.execute("BEGIN IMMEDIATE")
    try:
        sid = (
            scope.global_scope_id(conn)
            if global_scope
            else scope.scope_id(conn, cwd)
        )
        found = [_cite(conn, ref, quote) for ref, quote in cites]
        if quote_only is not None:
            found.append(_caller_prompt(conn, root, quote_only))
        found = list({(c["event"], c["quote"]): c for c in found}.values())
        _proposals(conn, found)
        if kind == "preference" and not any(
            (c["role"], c["kind"]) == ("user", "prompt") for c in found
        ):
            raise RefusedError("preference_needs_user")
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


@_guarded
def verify_citation(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """ok | changed | missing | erased for one citation row.

    erased: the citation was scrubbed. changed: the cited line is now
    another line (its hash differs or the quote is gone) or no longer
    exists in a live source. missing: the provider deleted the source (the
    event is kept) or nothing is left to look at. ok: the event is there
    with the same hash and the quote is still in its text.
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
    quoted = query.quote_check(conn, str(event["id"]), row["quote"])["match"]
    if event["line_sha256"] != row["line_sha256"] or not quoted:
        return "changed"
    return "missing" if event["status"] == "missing" else "ok"


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
        raise RefusedError("bad_actor")
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
            raise RefusedError("not_found")
        if row["status"] != "current":
            raise RefusedError("not_current")
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


@_guarded
def check(conn: sqlite3.Connection) -> dict:
    """Re-verify every citation: counts of ok, changed, missing, erased and
    the changed/missing ones named by entry and ref (never text)."""
    counts = dict.fromkeys(("ok", "changed", "missing", "erased"), 0)
    problems = []
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
    out = {"notice": NOTICE, "citations": len(rows), **counts}
    out["problems"] = problems[:PROBLEMS_MAX]
    if len(problems) > PROBLEMS_MAX:
        out["problems_omitted"] = len(problems) - PROBLEMS_MAX
    return out


@_guarded
def block_entries(
    conn: sqlite3.Connection, scope_ids: list[int], limit: int = 8
) -> list[dict]:
    """What the SessionStart block may push (spec O5b): current entries of
    these scopes with at least one live user-prompt citation, newest first,
    each with its actor and the first such verbatim quote, cut to
    BLOCK_QUOTE characters. Reply- and tool-only-cited entries stay
    pull-only."""
    if not scope_ids or limit < 1:
        return []
    rows = conn.execute(
        "SELECT k.id, k.kind, k.text, k.actor, k.created_at, sc.label,"
        " c.provider, c.thread_id, c.line, c.part, c.quote"
        " FROM knowledge k JOIN scope sc ON sc.id = k.scope_id"
        " JOIN citation c ON c.id = (SELECT min(m.id) FROM citation m"
        f" WHERE m.knowledge_id = k.id AND {_PUSHABLE_CITE})"
        f" WHERE k.status = 'current' AND k.scope_id IN"
        f" ({','.join('?' * len(scope_ids))})"
        " ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
        [*scope_ids, limit],
    )
    return [
        {
            "id": f"K{r['id']}",
            "kind": r["kind"],
            "scope": r["label"],
            "text": r["text"],
            "actor": r["actor"],
            "date": _date(r["created_at"]),
            "cite": _ref(r),
            "quote": r["quote"][:BLOCK_QUOTE],
        }
        for r in rows
    ]


@_guarded
def user_cited(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """Which of these entry numbers may be pushed (spec O5b): those with at
    least one live user-prompt citation, the rule block_entries applies."""
    rows = conn.execute(
        "SELECT k.id FROM knowledge k WHERE k.id IN"
        f" ({','.join('?' * len(ids))}) AND EXISTS (SELECT 1 FROM citation m"
        f" WHERE m.knowledge_id = k.id AND {_PUSHABLE_CITE})",
        ids,
    )
    return {row[0] for row in rows}


def run_add(home: Path, *, wait_s: float = 15.0, **kw) -> dict:
    """Take the writer lock, open the store (fullfsync on, O4d), add."""
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return add(conn, **kw)
        finally:
            conn.close()


def run_retract(home: Path, *, wait_s: float = 15.0, **kw) -> dict:
    """Take the writer lock, open the store (fullfsync on, O4d), retract."""
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return retract(conn, **kw)
        finally:
            conn.close()
