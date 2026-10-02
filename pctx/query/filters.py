"""Build the WHERE fragments that decide which events a query may see."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from pctx.query.answers import BadArgumentError
from pctx.query.constants import ALL_KINDS, DEFAULT_KINDS, PROVIDERS
from pctx.query.guard import guarded

# SQL fragments and the parameters that belong to them, in matching order.
type Where = tuple[list[str], list[Any]]

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\w:.+-]*)?")
# Subagent reports are stored in the parent's thread as harness events, so
# they have to be told apart from the parent's own harness lines by tag.
AGENT_REPORT = "(e.kind = 'harness' AND COALESCE(e.tag, '') = 'agent_message')"


def root_of(conn: sqlite3.Connection, ident: str) -> str:
    """Return the session root of a thread id; an unknown id is a root."""
    row = conn.execute(
        "SELECT session_root FROM source WHERE thread_id = ? LIMIT 1", (ident,)
    ).fetchone()
    return str(row[0]) if row else ident


@guarded
def caller_root(
    conn: sqlite3.Connection, env: Mapping[str, str]
) -> str | None:
    """Return the calling agent's session root from its environment.

    CLAUDE_CODE_SESSION_ID, else CODEX_SESSION_ID (both are roots), else
    CODEX_THREAD_ID mapped to its session_root through the source table.

    Args:
        conn: Read-only store connection.
        env: The caller's environment variables.

    Returns:
        The session root, or None when the environment names none.
    """
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID"):
        if env.get(name):
            return env[name]
    thread = env.get("CODEX_THREAD_ID")
    return root_of(conn, thread) if thread else None


def resolve_session(conn: sqlite3.Connection, given: str) -> str:
    """Return a session root from a root or an unambiguous prefix of one.

    Args:
        conn: Read-only store connection.
        given: A session root or a prefix of one.

    Returns:
        The matching session root.

    Raises:
        BadArgumentError: ``unknown_session`` when nothing matches or
            ``given`` is empty, ``ambiguous_session`` when a prefix matches
            several roots.
    """
    if not given:
        raise BadArgumentError("unknown_session")
    # No ORDER BY, so LIMIT 3 could cut an exact root that has 3+ longer
    # prefix matches. Session roots are all UUIDs of equal length, so an
    # exact match is also the only possible prefix match.
    found = conn.execute(
        "SELECT DISTINCT session_root FROM source WHERE session_root = ?"
        " OR substr(session_root, 1, ?) = ? LIMIT 3",
        (given, len(given), given),
    ).fetchall()
    exact = [row[0] for row in found if row[0] == given]
    if exact:
        return str(exact[0])
    if len(found) > 1:
        raise BadArgumentError("ambiguous_session")
    if not found:
        raise BadArgumentError("unknown_session")
    return str(found[0][0])


def _iso(value: str) -> str:
    """Return the value if it is an ISO date or timestamp, else refuse.

    Args:
        value: Text to check.

    Returns:
        The value, unchanged.

    Raises:
        BadArgumentError: ``bad_date`` when it is not an ISO date or
            timestamp.
    """
    try:
        if _DATE.fullmatch(value):
            date.fromisoformat(value[:10])
            return value
    except ValueError:
        pass
    raise BadArgumentError("bad_date")


def times(since: str | None, until: str | None) -> Where:
    """Return clauses for ts >= since and ts up to the end of ``until``.

    Args:
        since: Earliest timestamp or date, or None.
        until: Latest timestamp or date (a bare day includes that whole
            day), or None.

    Returns:
        The SQL clauses and their parameters. A malformed bound raises
        BadArgumentError (``bad_date``) through ``_iso``.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("e.ts >= ?")
        params.append(_iso(since))
    if until is not None:
        value = _iso(until)
        if len(value) == 10:  # a bare day runs to its last moment
            value = (date.fromisoformat(value) + timedelta(1)).isoformat()
            clauses.append("e.ts < ?")
        else:
            clauses.append("e.ts <= ?")
        params.append(value)
    return clauses, params


def _kind_clause(kinds: set[str] | None, subagents: bool) -> Where:
    """Return the event-kind predicate for the requested kinds.

    Args:
        kinds: Requested kinds; empty or None means the defaults.
        subagents: Add delegations and agent reports to the defaults.

    Returns:
        The SQL clause and its parameters.

    Raises:
        BadArgumentError: ``bad_kind`` when a kind is not recognised.
    """
    if kinds:
        if not kinds <= set(ALL_KINDS):
            raise BadArgumentError("bad_kind")
        want, extra = sorted(kinds), ""
    else:
        want = list(DEFAULT_KINDS + (("delegation",) if subagents else ()))
        extra = f" OR {AGENT_REPORT}" if subagents else ""
    marks = ",".join("?" * len(want))
    return [f"(e.kind IN ({marks}){extra})"], list(want)


def eligible(
    kinds: set[str] | None,
    provider: str | None,
    when: Where,
    subagents: bool,
    session_root: str | None,
    me: str | None = None,
) -> Where:
    """Return WHERE fragments over e (event) and s (source), except scope.

    Args:
        kinds: Event kinds to match; empty or None means the defaults.
        provider: Restrict to one provider, or None for both.
        when: Clauses from ``times``.
        subagents: Include subagent threads and the reports they leave.
        session_root: Restrict to exactly this session, or None.
        me: The caller's own session, which is left out unless it is the
            session explicitly asked for.

    Returns:
        The clauses and their parameters in matching order.

    Raises:
        BadArgumentError: If a kind or provider is not recognised.
    """
    if subagents:
        clauses = ["s.thread_class IN ('primary', 'subagent')"]
    else:
        clauses = ["s.thread_class = 'primary'", f"NOT {AGENT_REPORT}"]
    kind_clauses, params = _kind_clause(kinds, subagents)
    # flags & 1 marks events held back from answers (flagged content).
    clauses += [*kind_clauses, "e.flags & 1 = 0"]
    if provider is not None:
        if provider not in PROVIDERS:
            raise BadArgumentError("bad_provider")
        clauses.append("s.provider = ?")
        params.append(provider)
    clauses += when[0]
    params += when[1]
    if me and me != session_root:
        clauses.append("s.session_root != ?")
        params.append(me)
    if session_root:
        clauses.append("s.session_root = ?")
        params.append(session_root)
    return clauses, params


def scope_clause(
    ids: list[int], cwd_exact: str | None, everywhere: bool
) -> tuple[str, list[Any]]:
    """Return the scope predicate over e: exact cwd, no filter, or ids."""
    if cwd_exact is not None:
        return "e.cwd = ?", [cwd_exact]
    if everywhere:
        return "1", []
    if not ids:
        return "0", []
    return f"e.scope_id IN ({','.join('?' * len(ids))})", list(ids)


def scope_label(
    conn: sqlite3.Connection,
    ids: list[int],
    exact: str | None,
    everywhere: bool,
) -> str | None:
    """Return the label that names the scope an answer was drawn from."""
    if exact is not None:
        return exact
    if everywhere:
        return "all"
    row = conn.execute(
        "SELECT label FROM scope WHERE kind != 'global' AND id IN"
        f" ({','.join('?' * len(ids))}) LIMIT 1",
        ids,
    ).fetchone()
    return str(row[0]) if row else None
