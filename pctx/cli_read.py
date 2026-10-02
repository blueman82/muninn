"""Command handlers that only read the store.

Each handler follows the contract in ``pctx.cli_core``.  Readers never write
the store; what they report about themselves goes into ``record`` and from
there into the call log as counts and ids, never text.
"""

from __future__ import annotations

import hashlib
import sqlite3
from argparse import Namespace
from pathlib import Path
from typing import Any

from pctx import ingest, obs, query
from pctx.cli_core import (
    Env,
    Out,
    Record,
    Result,
    code_for,
    current_dir,
    read_store,
)

__all__ = [
    "doctor",
    "open_event",
    "quote_check",
    "search",
    "session",
    "sessions",
    "stats",
]


def _split_kinds(raw: str | None) -> set[str] | None:
    """Parse ``--kind a,b`` into a set, ignoring empty items."""
    return set(filter(None, raw.split(","))) if raw else None


def search(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx search`` and log hashed query, term count and ids."""
    status = obs.read_status(home)
    kinds = _split_kinds(a.kind)

    def work(conn: sqlite3.Connection) -> Out:
        return query.search(
            conn,
            a.query,
            cwd=current_dir(env),
            env=env,
            all_projects=a.all_projects,
            kinds=kinds,
            provider=a.provider,
            since=a.since,
            until=a.until,
            session=a.session,
            recent=a.recent,
            limit=a.limit,
            page=a.page,
            include_current=a.include_current,
            scope=a.scope,
            include_subagents=a.include_subagents,
            current_session=a.current_session,
            status=status,
        )

    out = read_store(home, work)
    fts = query.build_fts_query(a.query)
    # The call log gets a 12-hex digest of the query, never its text.
    record |= {
        "n_terms": fts.count(" OR ") + 1 if fts else 0,
        "query_sha12": hashlib.sha256(a.query.encode()).hexdigest()[:12],
        "stages": out.get("stages", {}),
        "returned_ids": [h.get("id") for h in out.get("hits", [])],
        "knowledge_ids": [k.get("id") for k in out.get("knowledge", [])],
    }
    return code_for(out), out


def open_event(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx open``: one event in full with its neighbours."""
    status = obs.read_status(home)

    def work(conn: sqlite3.Connection) -> Out:
        return query.open_event(
            conn,
            a.ref,
            roots=ingest.default_roots(env),
            context=a.context,
            offset=a.offset,
            raw=a.raw,
            status=status,
        )

    out = read_store(home, work)
    record |= {
        "target_id": out.get("id"),
        "n_context": len(out.get("neighbours", [])),
        "hash_ok": out.get("hash_ok"),
    }
    return code_for(out), out


def sessions(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx sessions``: sessions in scope, newest first."""
    status = obs.read_status(home)

    def work(conn: sqlite3.Connection) -> Out:
        return query.sessions(
            conn,
            cwd=current_dir(env),
            all_projects=a.all_projects,
            since=a.since,
            limit=a.limit,
            status=status,
        )

    out = read_store(home, work)
    return code_for(out), out


def session(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx session``: one session's events across its threads."""
    status = obs.read_status(home)

    def work(conn: sqlite3.Connection) -> Out:
        return query.session(
            conn, a.root, from_id=a.from_id, limit=a.limit, status=status
        )

    out = read_store(home, work)
    return code_for(out), out


def quote_check(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx quote-check``: is the quote verbatim in the event?"""

    def work(conn: sqlite3.Connection) -> Out:
        return query.quote_check(conn, a.ref, a.quote)

    out = read_store(home, work)
    return code_for(out), out


def stats(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx stats``: counts, and usage with ``--usage``."""

    def work(conn: sqlite3.Connection) -> Out:
        return obs.stats(conn, home, env, usage=a.usage)

    return 0, read_store(home, work)


def doctor(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run ``pctx doctor``; exit 1 when any check fails."""
    out: dict[str, Any] = obs.doctor(home, env)
    record["counts"] = {"failed": sum(c["ok"] is False for c in out["checks"])}
    return (0 if out["ok"] else 1), out
