"""Open the store for one short learning write."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from muninn import store
from muninn.review_catchup import catch_up
from muninn.review_models import ReviewPolicy


def is_contention(error: sqlite3.OperationalError) -> bool:
    """Recognize only SQLite's primary busy and locked result codes."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


@contextmanager
def database(
    home: Path, *, transaction: bool = True
) -> Generator[sqlite3.Connection]:
    """Close SQLite before releasing the non-reentrant writer descriptor."""
    with (
        store.writer_lock(home, wait_s=0),
        closing(store.connect_rw(store.db_path(home))) as conn,
    ):
        if transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if transaction:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise


def catch_up_due(
    home: Path,
    policy: ReviewPolicy,
    now: float,
    state: tuple[int, float],
) -> tuple[int, float]:
    """Give one source a turn between closed writer transactions."""
    after, due = state
    if now < due:
        return state
    with database(home) as conn:
        after = catch_up(conn, policy, now, after)
    return after, now + 5.0
