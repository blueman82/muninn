"""Plumbing shared by the pctx command handlers.

A handler is ``handler(args, env, home, record) -> (exit_code, output)``.
``record`` is the allowlisted call-log entry the handler may add counts and
ids to; ``output`` is the JSON object to print, or ``None`` when the command
prints nothing (the poller).
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pctx import store

__all__ = [
    "WRITER_WAIT_S",
    "Env",
    "Handler",
    "Out",
    "Record",
    "Result",
    "code_for",
    "counts",
    "current_dir",
    "knowledge_id",
    "read_store",
]

# CLI writers wait this long for the writer lock.  The poller never waits
# (it skips the pass), so a human command wins within one poll interval.
WRITER_WAIT_S = 15.0

type Env = Mapping[str, str]
type Out = dict[str, Any]
type Record = dict[str, Any]
type Result = tuple[int, Out | None]
type Handler = Callable[[Namespace, Env, Path, Record], Result]


def _read_once[T](db: Path, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``work`` on a fresh read-only connection, always closing it."""
    conn = store.connect_ro(db)
    try:
        return work(conn)
    finally:
        conn.close()


def read_store[T](home: Path, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``work`` on a read-only connection to the store.

    A hot journal is healed once when this process is allowed to write;
    otherwise the error propagates and the caller reports exit 4.  Readers
    never write, so healing is the only write a read command can trigger.

    Args:
        home: Data directory that holds the store.
        work: Callback that receives the open connection.

    Returns:
        Whatever ``work`` returns.

    Raises:
        HotJournalError: If the journal could not be healed, or the second
            attempt still finds one.
    """
    db = store.db_path(home)
    try:
        return _read_once(db, work)
    except store.HotJournalError:
        if not store.heal_hot_journal(db, home):
            raise
    return _read_once(db, work)


def current_dir(env: Env) -> str:
    """Return the working directory, falling back to ``$PWD`` if deleted."""
    try:
        return str(Path.cwd())
    except OSError:  # the cwd was deleted
        return env.get("PWD", "")


def code_for(out: Out) -> int:
    """Return exit code 2 for an error answer, else 0."""
    return 2 if "error" in out else 0


def counts(out: Mapping[str, Any]) -> dict[str, int]:
    """Return the integer fields of ``out``; booleans are not counts."""
    return {
        k: v
        for k, v in out.items()
        if isinstance(v, int) and not isinstance(v, bool)
    }


def knowledge_id(entry: Mapping[str, Any]) -> int:
    """Return the numeric part of an entry id such as ``K12``."""
    return int(entry["id"][1:])
