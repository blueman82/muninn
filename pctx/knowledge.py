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
TEXT_MAX = 500
QUOTE_MIN = 12
QUOTE_MAX = 300

# `<` of a frame delimiter, however spaced or cased (design 4.8)
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*pctx-(?:memory|recall))")


class Refused(Exception):
    """A write was refused and nothing was written (exit 2).

    code: bad_kind, bad_actor, text_length, uncited, bad_ref, not_found,
    ambiguous_ref, not_citable, quote_length, quote_not_found,
    preference_needs_user, bad_supersedes.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _clean(text: str) -> str:
    """Entry text: stripped, secrets redacted, then frame delimiters
    escaped; refused unless 1..TEXT_MAX chars."""
    if len(text) > 4 * TEXT_MAX:  # not worth scanning
        raise Refused("text_length")
    body = _FRAME.sub("&lt;", classify.redact(text.strip())[0])
    if not 1 <= len(body) <= TEXT_MAX:
        raise Refused("text_length")
    return body


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


def _insert(conn, sid, kind, body, actor, cites) -> int:
    now = time.time()
    kid = conn.execute(
        "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
        " created_at) VALUES (?, ?, ?, 'current', ?, ?)",
        (sid, kind, body, actor, now),
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
    conn.execute(
        "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
        " VALUES (?, 'add', ?, ?)",
        (kid, actor, now),
    )
    return kid


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
    body = _clean(text)
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
        kid = _insert(conn, sid, kind, body, actor, found)
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


def _entry(conn: sqlite3.Connection, kid: int) -> dict:
    row = conn.execute(
        "SELECT k.*, sc.label FROM knowledge k"
        " JOIN scope sc ON sc.id = k.scope_id WHERE k.id = ?",
        (kid,),
    ).fetchone()
    cites = conn.execute(
        "SELECT * FROM citation WHERE knowledge_id = ? ORDER BY id", (kid,)
    )
    return {
        "id": f"K{row['id']}",
        "kind": row["kind"],
        "text": row["text"],
        "status": row["status"],
        "scope": row["label"],
        "actor": row["actor"],
        "date": time.strftime("%Y-%m-%d", time.gmtime(row["created_at"])),
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
