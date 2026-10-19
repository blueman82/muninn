"""Explicit initialization for never-exposed disposable test homes."""

from __future__ import annotations

import os
from pathlib import Path

from muninn import review_quiescence, store, tombstones
from muninn import review_quiescence_state as state


def initialize(home: Path) -> None:
    """Seed only a new fixture whose creation and nonexposure the test owns."""
    fd = state.acquire(home)
    try:
        with store.writer_lock(home, wait_s=0):
            store.connect_rw(store.db_path(home)).close()
            state.seed_new(home, fd)
    finally:
        os.close(fd)


def interrupt_erasure(home: Path, thread_id: str) -> bytes:
    """Leave real durable intent unapplied after an original caller error."""
    before = (home / state.WITNESS).read_bytes()
    status = review_quiescence.EraseStatus()
    original = LookupError("caller failed after append")
    try:
        with review_quiescence.erase_connection(
            home, wait_s=0, status=status
        ) as conn:
            tombstones.append_tombstones(
                home,
                [
                    tombstones.tombstone_row(
                        "codex", "thread", thread_id=thread_id
                    )
                ],
            )
            status.erasure_phase = "intent_durable"
            assert (
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0]
                == 0
            )
            raise original
    except LookupError as caught:
        assert caught is original
    assert status.erasure_phase == "intent_durable"
    assert (home / state.WITNESS).read_bytes() == before
    return before
