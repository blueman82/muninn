"""Open the store for a maintenance write under the writer lock."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Generator
from pathlib import Path

from muninn import store
from muninn.tombstones import reapply_tombstones


@contextlib.contextmanager
def maintenance_connection(
    home: Path, *, wait_s: float
) -> Generator[sqlite3.Connection]:
    """Yield a read-write store with the writer lock held.

    Recorded erasure is finished first, so a crash between an erase intent
    and its commit cannot bring erased text back (ADR 0012).

    Args:
        home: Data directory.
        wait_s: How long to wait for the writer lock.

    Yields:
        An autocommit read-write connection, closed on exit.

    Raises:
        store.StoreUnavailableError: If there is no store.
        store.BusyError: If the writer lock stays busy for ``wait_s``.
    """
    if not store.db_path(home).is_file():
        raise store.StoreUnavailableError("store is absent")
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            reapply_tombstones(conn, home)
            yield conn
        finally:
            conn.close()
