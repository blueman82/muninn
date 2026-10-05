"""Command handlers for ``muninn know``, the cited knowledge ledger."""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from muninn import cli_core, ingest, knowledge, obs
from muninn.cli_core import (
    Env,
    Handler,
    Out,
    Record,
    Result,
    code_for,
    counts,
    current_dir,
    knowledge_id,
    read_store,
)

__all__ = ["know"]


def _citations(
    cited: Sequence[tuple[str, str]] | None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Pair up ``add``'s ordered ``--cite``/``--quote`` flags.

    Args:
        cited: ``("cite" | "quote", value)`` pairs in command-line order.

    Returns:
        The ``(ref, quote)`` pairs, and the one quote given without a cite.

    Raises:
        ValueError: If a cite lacks its quote, or two quotes stand alone.
    """
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    alone: list[str] = []
    for what, value in cited or ():
        if what == "cite":
            if pending is not None:
                raise ValueError("each --cite needs its --quote")
            pending = value
        elif pending is not None:
            pairs.append((pending, value))
            pending = None
        else:
            alone.append(value)
    if pending is not None:
        raise ValueError("each --cite needs its --quote")
    if len(alone) > 1:
        raise ValueError("only one --quote may stand without --cite")
    return pairs, (alone[0] if alone else None)


def _refused(refused: knowledge.RefusedError) -> Result:
    """Turn a refused ledger write into exit 2 with its code."""
    return 2, {"error": refused.code}


def _add(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Record a new entry; every entry needs a verbatim quote."""
    pairs, alone = _citations(a.cited)
    try:
        out: Out = knowledge.run_add(
            home,
            # Looked up on cli_core at call time so tests can patch it.
            wait_s=cli_core.WRITER_WAIT_S,
            kind=a.kind,
            text=a.text,
            cites=pairs,
            quote_only=alone,
            supersedes=a.supersedes,
            global_scope=a.is_global,
            confidence=a.confidence,
            valid_until=a.valid_until,
            sensitivity=a.sensitivity,
            contradicts=a.contradicts,
            tags=a.tags,
            loop=a.loop,
            cwd=current_dir(env),
            actor=obs.actor(env),
            roots=ingest.default_roots(env),
            env=env,
        )
    except knowledge.RefusedError as refused:
        return _refused(refused)
    record["knowledge_ids"] = [knowledge_id(out["entry"])]
    return 0, out


def _retract(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Retract a current entry."""
    try:
        out: Out = knowledge.run_retract(
            home,
            wait_s=cli_core.WRITER_WAIT_S,
            kid=a.kid,
            reason=a.reason,
            actor=obs.actor(env),
        )
    except knowledge.RefusedError as refused:
        return _refused(refused)
    record["knowledge_ids"] = [knowledge_id(out["entry"])]
    return 0, out


def _list(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """List entries of this repo and global, newest first."""
    if a.loop is not None and a.all_projects:  # a loop is one scope
        return 2, {"error": "loop_with_all_projects"}

    def work(conn: sqlite3.Connection) -> Out:
        return knowledge.list_entries(
            conn,
            cwd=current_dir(env),
            status=a.status,
            kind=a.kind,
            all_projects=a.all_projects,
            loop=a.loop,
        )

    out = read_store(home, work)
    entries: list[dict[str, Any]] = out.get("entries", [])
    record["knowledge_ids"] = [knowledge_id(e) for e in entries]
    return code_for(out), out


def _show(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Show one entry with its chain and log."""

    def work(conn: sqlite3.Connection) -> Out:
        return knowledge.show(conn, a.kid)

    out = read_store(home, work)
    if "entry" in out:
        record["knowledge_ids"] = [knowledge_id(out["entry"])]
    return code_for(out), out


def _check(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Re-verify every citation against its transcript."""
    out: Out = read_store(home, knowledge.check)
    record["counts"] = counts(out)
    return 0, out


_SUBCOMMANDS: dict[str, Handler] = {
    "add": _add,
    "retract": _retract,
    "list": _list,
    "show": _show,
    "check": _check,
}


def know(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Dispatch ``muninn know SUBCOMMAND``; log it as ``know SUBCOMMAND``."""
    record["cmd"] = f"know {a.know_cmd}"
    return _SUBCOMMANDS[a.know_cmd](a, env, home, record)
