"""Create disposable store homes and leave erasure intent unapplied."""

from __future__ import annotations

from pathlib import Path

from muninn import store, tombstones


def initialize(home: Path) -> None:
    """Create an empty store in a disposable test home."""
    with store.writer_lock(home, wait_s=0):
        store.connect_rw(store.db_path(home)).close()


def interrupt_erasure(home: Path, thread_id: str) -> None:
    """Leave durable erasure intent unapplied, as a crash would."""
    with store.writer_lock(home, wait_s=0):
        tombstones.append_tombstones(
            home,
            [tombstones.tombstone_row("codex", "thread", thread_id=thread_id)],
        )
