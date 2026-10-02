"""Ranked search over events and knowledge."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from typing import Any, Required, TypedDict, Unpack

from muninn.query.answers import Answer, BadArgumentError, error
from muninn.query.constants import NOTICE, PREVIEW_NOTICE
from muninn.query.filters import (
    Where,
    caller_root,
    eligible,
    resolve_session,
    root_of,
    scope_clause,
    scope_label,
    times,
)
from muninn.query.guard import guarded
from muninn.query.hits import (
    CANDIDATES,
    CANDIDATES_SQL,
    compose,
    knowledge,
    render,
    tally,
)
from muninn.query.index_age import freshness
from muninn.query.paging import paginate
from muninn.query.terms import build_fts_query
from muninn.scope import scope_ids_for_read

PAGE_MAX = 30
RECENT_POOL = 50  # entries re-sorted by time when recent=True
OTHER_SCOPES = 5  # labels listed in other_scopes


class SearchArgs(TypedDict, total=False):
    """Keyword arguments of ``search`` after the connection and the query."""

    cwd: Required[str]
    env: Required[Mapping[str, str]]
    all_projects: bool
    kinds: set[str] | None
    provider: str | None
    since: str | None
    until: str | None
    session: str | None
    recent: bool
    limit: int
    page: int
    include_current: bool
    scope: str | None
    include_subagents: bool
    current_session: str | None
    status: Mapping[str, Any] | None


def _eligibility(
    conn: sqlite3.Connection, args: SearchArgs
) -> tuple[str | None, Where]:
    """Resolve the session filter and build the eligibility clauses.

    Args:
        conn: Read-only store connection.
        args: The keyword arguments of ``search``.

    Returns:
        The resolved session root (or None) and the eligibility clauses.

    Raises:
        BadArgumentError: If the session, a kind, the provider or a date is
            invalid; ``search`` turns it into an error answer.
    """
    session = args.get("session")
    root = resolve_session(conn, session) if session else None
    me = None
    if not args.get("include_current", False):
        current = args.get("current_session")
        me = current and root_of(conn, current)
        me = me or caller_root(conn, args["env"])
    return root, eligible(
        set(args.get("kinds") or ()),
        args.get("provider"),
        times(args.get("since"), args.get("until")),
        args.get("include_subagents", False),
        root,
        me,
    )


def _other_scopes(other: Counter[str]) -> dict[str, int]:
    """Return the biggest outside scopes, ties broken by label."""
    ranked = sorted(other.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ranked[:OTHER_SCOPES])


def _check_keywords(args: Mapping[str, object]) -> None:
    """Reject unknown or missing keywords, much like a signature would.

    Unlike a real signature it reports only the first missing keyword.

    ``search`` takes ``**args`` only because a signature with this many
    parameters trips the argument-count limit; the type checker sees the
    keywords through ``SearchArgs``, so the runtime check lives here.

    Args:
        args: The keyword arguments passed to ``search``.

    Raises:
        TypeError: A keyword is unknown, or ``cwd``/``env`` is missing.
    """
    for name in args:
        if name not in SearchArgs.__annotations__:
            raise TypeError(
                f"search() got an unexpected keyword argument {name!r}"
            )
    # Spelled out: with postponed annotations TypedDict cannot see Required.
    for name in ("cwd", "env"):
        if name in args:
            continue
        raise TypeError(
            f"search() missing required keyword-only argument: {name!r}"
        )


@guarded
def search(
    conn: sqlite3.Connection, query: str, **args: Unpack[SearchArgs]
) -> Answer:
    """Return ranked events and knowledge: OR of terms, bm25.

    Default scope is the repo of cwd (worktrees and subdirs fold in) plus
    global knowledge; ``scope`` narrows to events whose cwd equals it
    exactly, ``all_projects`` drops the filter. The caller's own session
    (env, or ``current_session``) is left out unless ``include_current``.
    ``session`` lists matches in one session (a root or unambiguous
    prefix) with no per-session cap; candidates are still limited to
    CANDIDATES and paged. Knowledge (page 1, not in a session listing) ignores
    kinds, provider and times. ``include_subagents`` adds subagent threads
    and the reports a parent thread stores as harness/agent_message.

    Args:
        conn: Read-only store connection.
        query: Free search text.
        **args: ``cwd`` and ``env`` are required. Optional: ``all_projects``,
            ``kinds``, ``provider``, ``since``, ``until``, ``session``,
            ``recent``, ``limit`` (default 10), ``page`` (default 1),
            ``include_current``, ``scope``, ``include_subagents``,
            ``current_session`` and ``status`` (a parsed status.json).

    Returns:
        The answer, or ``{"error": code}`` for bad input.

    Raises:
        TypeError: If a keyword is unknown or ``cwd`` or ``env`` is missing.
        StoreUnavailableError: If the store cannot be read (through
            ``guarded``).
        HotJournalError: If a crashed writer left a journal (through
            ``guarded``).
    """
    _check_keywords(args)
    limit = min(max(args.get("limit", 10), 1), PAGE_MAX)
    page = max(args.get("page", 1), 1)
    status = args.get("status")
    fts = build_fts_query(query)
    out: Answer = {
        "notice": NOTICE,
        "preview_notice": PREVIEW_NOTICE,
        "knowledge": [],
        "hits": [],
    }
    if fts is None:
        return out | {"note": "no searchable terms", **freshness(status)}
    try:
        root, (clauses, params) = _eligibility(conn, args)
    except BadArgumentError as bad:
        return error(str(bad))
    scope, everywhere = args.get("scope"), args.get("all_projects", False)
    ids = scope_ids_for_read(conn, scope or args["cwd"])
    inside = scope_clause(ids, scope, everywhere)
    where = " AND ".join(clauses)
    rows = conn.execute(
        CANDIDATES_SQL.format(
            where=f"{where} AND {inside[0]}", limit=CANDIDATES
        ),
        [fts, *params, *inside[1]],
    ).fetchall()
    total, other = tally(conn, fts, where, params, inside)
    entries, session_capped, tool_capped = compose(
        rows,
        limit,
        per_session=root is None,
        tools=not args.get("kinds") and root is None,
    )
    pool = entries
    if args.get("recent"):
        pool = sorted(
            entries[:RECENT_POOL],
            key=lambda e: e["row"]["ts"] or "",
            reverse=True,
        )
    hits = render(conn, fts, pool, entries, total)
    out |= {
        "scope": scope_label(conn, ids, scope, everywhere),
        "hits": hits,
        "page": page,
        "limit": limit,
        "other_scopes": _other_scopes(other),
        "stages": {
            "matches": sum(total.values()) + sum(other.values()),
            "in_scope": sum(total.values()),
            "candidates": len(rows),
            "session_capped": session_capped,
            "tool_capped": tool_capped,
            "returned": len(hits),
        },
    }
    if root is None:
        out["knowledge"] = knowledge(conn, fts, ids, everywhere)
    if not total and other:
        out["note"] = (
            f"0 matches in this scope; {sum(other.values())} matches outside"
            " this scope; use --all-projects"
        )
    return paginate(hits, out | freshness(status), limit, page)
