"""Replay checked recovery work for an interrupted review pause."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

from muninn.review_requests import recover_attempts
from muninn.tombstones import reapply_tombstones


def recover_paused_store(conn: sqlite3.Connection, home: Path) -> None:
    """Fence old attempts, then apply durable deletion requests.

    Args:
        conn: The store connection held under the maintenance barriers.
        home: Muninn's private data directory.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE review_control SET generation=generation+1")
        generation = conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        recover_attempts(conn, generation, time.time())
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    reapply_tombstones(conn, home)
