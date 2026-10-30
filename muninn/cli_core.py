"""Plumbing shared by the muninn command handlers.

A handler is ``handler(args, env, home, record) -> (exit_code, output)``.
``record`` is the allowlisted call-log entry the handler may add counts and
ids to; ``output`` is the JSON object to print, or ``None`` when the command
prints nothing (the poller).
"""

from __future__ import annotations

import sqlite3
from argparse import ArgumentTypeError, Namespace
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from muninn import store
from muninn.decision_models import DecisionId
from muninn.knowledge_models import KnowledgeId, RequestId, UnresolvedId

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

# CLI writers wait this long for the writer lock, which a poller pass may
# hold.  The poller itself never waits: it skips the pass instead.
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


def knowledge_argument(value: str) -> KnowledgeId:
    """Require the typed knowledge UUID rather than an integer alias."""
    try:
        return TypeAdapter(KnowledgeId).validate_python(value, strict=True)
    except ValidationError:
        raise ArgumentTypeError("invalid_knowledge_id") from None


def request_argument(value: str) -> RequestId:
    """Require the typed review request UUID."""
    try:
        return TypeAdapter(RequestId).validate_python(value, strict=True)
    except ValidationError:
        raise ArgumentTypeError("invalid_request_id") from None


def knowledge_id(entry: Mapping[str, Any]) -> KnowledgeId:
    """Return a validated opaque knowledge identity for content-free logs."""
    return TypeAdapter(KnowledgeId).validate_python(entry["id"], strict=True)


def write_store[T](home: Path, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run one checked writer operation under the existing shared lock."""
    with (
        store.writer_lock(home, wait_s=WRITER_WAIT_S),
        closing(store.connect_rw(store.db_path(home))) as conn,
    ):
        return work(conn)


def decision_argument(value: str) -> DecisionId:
    """Require the exact opaque decision identity."""
    try:
        return TypeAdapter(DecisionId).validate_python(value, strict=True)
    except ValidationError:
        raise ArgumentTypeError("invalid_decision_id") from None


def unresolved_argument(value: str) -> UnresolvedId:
    """Require the exact opaque unresolved-question identity."""
    try:
        return TypeAdapter(UnresolvedId).validate_python(value, strict=True)
    except ValidationError:
        raise ArgumentTypeError("invalid_unresolved_id") from None
