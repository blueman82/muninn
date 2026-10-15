"""Durable recall delivery metadata and review checkpoints, without text."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from muninn import classify, platform_io, store

BATCH = 50
NAME = "recall-review.sqlite"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS delivery ("
    "id INTEGER PRIMARY KEY, at REAL NOT NULL, metadata TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY,"
    " reviewed INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO checkpoint VALUES (1, 0)",
)


@contextmanager
def connection(home: Path, *, write: bool) -> Generator[sqlite3.Connection]:
    """Open the separate private audit database and close it reliably.

    Args:
        home: Muninn data directory.
        write: Create and serialize a metadata transaction when true.

    Yields:
        Connection inside a transaction; writes commit on normal exit.

    Raises:
        OSError: If audit file privacy or journal mode is unsafe.
    """
    path = home / NAME
    store.assert_sqlite_private(path)
    if write:
        fd = platform_io.open_private(path, os.O_CREAT | os.O_RDWR)
        os.close(fd)
    else:
        with platform_io.open_regular(path, root=home):
            if not platform_io.is_private(path):
                raise OSError("unsafe recall review database")
    with contextlib.closing(
        sqlite3.connect(
            path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=0.2,
            isolation_level=None,
        )
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise OSError("unsafe recall review journal mode")
        with conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                for statement in _SCHEMA:
                    conn.execute(statement)
            yield conn


def fingerprint(text: str) -> str:
    """Hash the redacted request exactly as the transcript index stores it."""
    return hashlib.sha256(classify.redact(text)[0].encode()).hexdigest()


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return the latest delivery id and the acknowledged batch count."""
    total = conn.execute(
        "SELECT COALESCE(max(id), 0) FROM delivery"
    ).fetchone()[0]
    reviewed = conn.execute(
        "SELECT reviewed FROM checkpoint WHERE id=1"
    ).fetchone()[0]
    return total, reviewed


def _feedback(number: int, reviewed: int) -> str:
    """Give the model a stable rating label and any overdue review command."""
    text = f"Tell human: Memory R{number}: useful/noise/uncertain — reason."
    if number // BATCH > reviewed:
        text += " Review due."
    return text


def capture(
    home: Path,
    payload: Mapping[str, object],
    scope_ids: list[int],
    render: Callable[[str], str],
    trace: dict[str, object],
) -> str:
    """Render and record one nonempty delivery, without blocking a prompt.

    Args:
        home: Existing private data directory.
        payload: Provider payload with a native muninn_provider value.
        scope_ids: Repository scope ids searched.
        render: Renderer that includes the supplied assessment instruction.
        trace: Selected and final source metadata, populated by render.

    Returns:
        Bounded recall context; ordinary recall if auditing is unavailable.
    """
    try:
        with connection(home, write=True) as conn:
            total, reviewed = counts(conn)
            number = total + 1
            text = render(_feedback(number, reviewed))
            if not text:
                return ""
            session = str(payload.get("session_id", ""))
            metadata = {
                "provider": payload["muninn_provider"],
                "session": (
                    session if re.fullmatch(r"[\w-]{1,128}", session) else None
                ),
                "prompt_sha256": fingerprint(str(payload["prompt"])),
                "scope_ids": scope_ids,
                "sources": trace.get("delivered_sources", []),
                "knowledge": trace.get("delivered_knowledge", []),
                "context_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "characters": len(text),
            }
            conn.execute(
                "INSERT INTO delivery VALUES (?, ?, ?)",
                (number, time.time(), json.dumps(metadata)),
            )
        trace["delivery_id"] = number
        if number // BATCH > reviewed:
            trace["review_due_batch"] = reviewed + 1
        return text
    except (OSError, sqlite3.Error):
        trace["review_error"] = 1
        return render(
            "Memory review unavailable. Tell the human whether this memory"
            " is useful, noise or uncertain."
        )


def review(home: Path, batch: int | None, *, ack: bool) -> dict[str, Any]:
    """Read one bounded batch or acknowledge the next completed batch.

    Args:
        home: Data directory holding metadata, never transcript text.
        batch: One-based batch; defaults to the oldest due or current batch.
        ack: Advance the checkpoint only for the next complete batch.

    Returns:
        Batch metadata, completion state, and delivery records.

    Raises:
        ValueError: If the batch or acknowledgement is invalid.
    """
    if not (home / NAME).exists():
        if ack or batch is not None:
            raise ValueError("no recall deliveries recorded")
        return {
            "total": 0,
            "reviewed_batches": 0,
            "batch": 1,
            "complete": False,
            "deliveries": [],
        }
    with connection(home, write=ack) as conn:
        total, reviewed = counts(conn)
        if not total and batch is None and not ack:
            return {
                "total": 0,
                "reviewed_batches": 0,
                "batch": 1,
                "complete": False,
                "deliveries": [],
            }
        selected = (
            batch
            if batch is not None
            else min(reviewed + 1, (total + BATCH - 1) // BATCH)
        )
        if selected < 1 or selected > (total + BATCH - 1) // BATCH:
            raise ValueError("batch has no deliveries")
        if ack:
            if selected != reviewed + 1 or selected * BATCH > total:
                raise ValueError(
                    "acknowledge the next complete batch after reviewing it"
                )
            conn.execute(
                "UPDATE checkpoint SET reviewed=? WHERE id=1", (selected,)
            )
            reviewed = selected
        rows = conn.execute(
            "SELECT * FROM delivery WHERE id BETWEEN ? AND ? ORDER BY id",
            ((selected - 1) * BATCH + 1, selected * BATCH),
        ).fetchall()
        return {
            "total": total,
            "reviewed_batches": reviewed,
            "batch": selected,
            "complete": selected * BATCH <= total,
            "deliveries": [
                {"id": r["id"], "at": r["at"], **json.loads(r["metadata"])}
                for r in rows
            ],
        }
