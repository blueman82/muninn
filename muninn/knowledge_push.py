"""Push side of the knowledge ledger: what may enter a prompt unasked.

A hook may push an entry only when it is current, not expired, not
restricted, and a live user-prompt citation whose quote backs the text.
Everything else is pull-only: still in ``know list`` and ``muninn search``.
The expiry and restriction predicates are ``PUSH_SQL``; the citation rule is
``PUSHABLE_CITE`` with ``text_backed``.  Readers use a ``connect_ro``
connection and never write.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from muninn.knowledge_expiry import LIVE_SQL, PUSH_SQL
from muninn.knowledge_model import (
    BLOCK_QUOTE,
    PULL_EXPIRED,
    PULL_ONLY,
    PULL_RESTRICTED,
    PUSHABLE_CITE,
    iso_date,
    text_backed,
)
from muninn.knowledge_read import cite_ref
from muninn.query import guarded

# Entries examined per wanted entry, and the most ever examined, so the
# hook's cost stays flat as the ledger grows; Python then drops entries whose
# quote does not back the text.  The scan counts entries, not citation rows,
# so one entry with many citations cannot crowd the rest out.  Beyond the cap
# an older entry is simply not pushed.
_OVERFETCH = 10
_SCAN_CAP = 200


@guarded
def block_entries(
    conn: sqlite3.Connection, scope_ids: list[int], limit: int = 8
) -> list[dict[str, Any]]:
    """Return the entries the SessionStart block may push.

    Only current entries that have not expired and are not restricted
    (``PUSH_SQL``), with a live user-prompt citation whose quote backs the
    entry text (``text_backed``), qualify; expired or restricted entries,
    entries cited by replies or tool calls alone, and entries whose text the
    user's words do not back stay pull-only.

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
    candidates = conn.execute(
        "SELECT k.id, k.kind, k.text, k.actor, k.created_at, sc.label"
        " FROM knowledge k JOIN scope sc ON sc.id = k.scope_id"
        f" WHERE k.status = 'current' AND {PUSH_SQL} AND k.scope_id IN"
        f" ({','.join('?' * len(scope_ids))}) AND EXISTS"
        " (SELECT 1 FROM citation m WHERE m.knowledge_id = k.id"
        f" AND {PUSHABLE_CITE})"
        " ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
        [time.time(), *scope_ids, min(limit * _OVERFETCH, _SCAN_CAP)],
    )
    out: list[dict[str, Any]] = []
    for k in candidates:
        cite = _backing_cite(conn, k["id"], k["text"])
        if cite is None:
            continue
        out.append(
            {
                "id": f"K{k['id']}",
                "kind": k["kind"],
                "scope": k["label"],
                "text": k["text"],
                "actor": k["actor"],
                "date": iso_date(k["created_at"]),
                "cite": cite_ref(cite),
                "quote": cite["quote"][:BLOCK_QUOTE],
            }
        )
        if len(out) == limit:
            break
    return out


def _backing_cite(
    conn: sqlite3.Connection, kid: int, text: str
) -> sqlite3.Row | None:
    """Return an entry's first user-prompt citation that backs its text."""
    # Iterated, not fetched: an entry with a great many citations is read
    # only as far as its first backing one.
    for cite in conn.execute(
        "SELECT m.provider, m.thread_id, m.line, m.part, m.quote"
        " FROM citation m WHERE m.knowledge_id = ?"
        f" AND {PUSHABLE_CITE} ORDER BY m.id",
        (kid,),
    ):
        if text_backed(text, cite["quote"] or ""):
            return cite
    return None


@guarded
def withheld(conn: sqlite3.Connection, scope_ids: list[int]) -> dict[str, int]:
    """Count current entries a push leaves out because of time or privacy.

    An expired entry counts only as ``expired``, even when it is also
    restricted.  Entries that are pull-only for lack of a backing user
    citation are in neither counter, so a zero count does not mean every
    entry in scope is pushed.

    Args:
        conn: Open store connection.
        scope_ids: Scopes a push draws from; none means nothing is counted.

    Returns:
        ``expired`` (past valid_until) and ``restricted`` (live but
        restricted) counts; numbers only, never entry text.
    """
    if not scope_ids:
        return {"expired": 0, "restricted": 0}
    row = conn.execute(
        f"SELECT sum(NOT {LIVE_SQL}), sum({LIVE_SQL} AND k.sensitivity ="
        " 'restricted') FROM knowledge k WHERE k.status = 'current'"
        f" AND k.scope_id IN ({','.join('?' * len(scope_ids))})",
        [time.time(), time.time(), *scope_ids],
    ).fetchone()
    return {"expired": row[0] or 0, "restricted": row[1] or 0}


@guarded
def user_cited(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """Return which entry ids may be pushed into a prompt.

    This is the same rule ``block_entries`` applies: not expired, not
    restricted (``PUSH_SQL``), and a live user-prompt citation whose quote
    backs the entry text.  It does not check the entry status.

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
        f" AND {PUSHABLE_CITE} AND {PUSH_SQL}",
        [*ids, time.time()],
    )
    return {r[0] for r in rows if text_backed(r[1], r[2] or "")}


def pull_only_note(conn: sqlite3.Connection, kid: int) -> str | None:
    """Say why an entry will not be pushed, or None when it will be.

    Args:
        conn: Open store connection.
        kid: Entry id.

    Returns:
        ``PULL_RESTRICTED`` for a restricted entry, ``PULL_EXPIRED`` for an
        expired one, ``PULL_ONLY`` when no user citation backs the text, and
        None when the entry is pushable.
    """
    row = conn.execute(
        "SELECT k.sensitivity, NOT " + LIVE_SQL + " FROM knowledge k"
        " WHERE k.id = ?",
        (time.time(), kid),
    ).fetchone()
    if row[0] == "restricted":
        return PULL_RESTRICTED
    if row[1]:
        return PULL_EXPIRED
    return None if kid in user_cited(conn, [kid]) else PULL_ONLY
