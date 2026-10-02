"""Citation checking for knowledge entries.

Every entry must point at the immutable identity of an event plus a verbatim
quote of it. These checks run inside the writer's transaction, so a failed
citation leaves nothing behind.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pctx import ingest, query
from pctx.knowledge_model import (
    CALLER_ENV,
    CITABLE,
    QUOTE_MAX,
    QUOTE_MIN,
    Cite,
    RefusedError,
    parse_kid,
)


def check_citation(conn: sqlite3.Connection, ref: str, quote: str) -> Cite:
    """Check one (ref, quote) pair and describe the citation.

    A quote under ``QUOTE_MIN`` characters is only accepted as the whole text
    of a user prompt: an approval (``"approval": True``), valid once the
    entry also cites the reply it answers (see ``require_proposals``).

    Args:
        conn: Open store connection.
        ref: Event reference to cite.
        quote: Text that must appear in the event, whitespace collapsed.

    Returns:
        The event's identity, the collapsed quote, its span in the event
        text and whether it is an approval.

    Raises:
        RefusedError: If the event is unknown or not citable, or the quote
            has a bad length, is missing, or is too short to stand alone.
    """
    opened: dict[str, Any] = query.open_event(conn, ref, roots={}, context=0)
    if "error" in opened:
        raise RefusedError(opened["error"])
    prov: dict[str, Any] = opened["provenance"]
    if (
        prov.get("class", "primary") != "primary"
        or prov["kind"] not in CITABLE
        or opened["flagged"]
    ):
        raise RefusedError("not_citable")
    wanted = " ".join(quote.split())
    if not wanted or len(wanted) > QUOTE_MAX:
        raise RefusedError("quote_length")
    found: dict[str, Any] = query.quote_check(conn, str(opened["id"]), quote)
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


def require_proposals(conn: sqlite3.Connection, cites: list[Cite]) -> None:
    """Require the reply right before each approval among the citations.

    A short approval ("yes") only means something next to the proposal it
    answers, which must be the previous reply in the same source.

    Args:
        conn: Open store connection.
        cites: The entry's checked citations.

    Raises:
        RefusedError: If an approval's preceding reply is not cited.
    """
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


def caller_session(
    conn: sqlite3.Connection,
    roots: Mapping[str, Path],
    env: Mapping[str, str],
) -> str | None:
    """Return the caller's session root, ingesting its threads first.

    The targeted ingest makes the prompt the caller just typed visible. It
    runs on this connection because the caller of ``add`` already holds the
    writer lock, which is not reentrant.

    Args:
        conn: Read-write connection under the writer lock.
        roots: Transcript roots by name; no roots skips the ingest.
        env: Environment naming the calling agent's session.

    Returns:
        The session root, or None when the caller is unknown.
    """
    ids = {env.get(name) for name in CALLER_ENV} - {None, ""}
    if ids and roots:
        # caller_root may return None, and the None stays in the set. Ingest
        # tests the set against each file's thread id, base id and session
        # root, and the base id is None for every file except a continuation
        # segment, so a None here matches nearly all sources and widens the
        # targeted ingest to a full pass. That widening is kept as is: it
        # only ever reads more, and the None case is an unknown caller. The
        # cast records that ingest's ``set[str]`` annotation is narrower
        # than what it accepts.
        only = cast(set[str], ids | {query.caller_root(conn, env)})
        ingest.ingest(conn, dict(roots), only_threads=only)
    return query.caller_root(conn, env)


def caller_prompt(conn: sqlite3.Connection, root: str, quote: str) -> Cite:
    """Cite the caller's latest citable user prompt that holds the quote.

    Args:
        conn: Open store connection.
        root: The caller's session root.
        quote: Text to find in one of the session's prompts.

    Returns:
        The checked citation for the newest matching prompt.

    Raises:
        RefusedError: If the quote has a bad length or no prompt holds it.
    """
    wanted = " ".join(quote.split())
    if not wanted or len(wanted) > QUOTE_MAX:
        raise RefusedError("quote_length")
    # The first word narrows the scan in SQL; check_citation does the exact
    # whitespace-collapsed match.
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
            return check_citation(conn, str(row["id"]), quote)
        except RefusedError:  # not this prompt: not citable, or other wording
            continue
    raise RefusedError("quote_not_found")


def supersedable(conn: sqlite3.Connection, given: object, sid: int) -> int:
    """Return the entry id a new entry may replace.

    Args:
        conn: Open store connection.
        given: An id in any form ``parse_kid`` accepts.
        sid: Scope of the new entry.

    Returns:
        The id of a current entry in the same scope.

    Raises:
        RefusedError: If there is no such current entry in that scope.
    """
    old = parse_kid(given)
    row = (
        conn.execute(
            "SELECT scope_id, status FROM knowledge WHERE id = ?", (old,)
        ).fetchone()
        if old
        else None
    )
    if old is None or row is None:
        raise RefusedError("bad_supersedes")
    if row["status"] != "current" or row["scope_id"] != sid:
        raise RefusedError("bad_supersedes")
    return old
