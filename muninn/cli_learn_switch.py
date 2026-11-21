"""Turn learning on or off for the serve process."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muninn import review_settings
from muninn.cli_core import Out, write_store
from muninn.review_activation import activate_sources


def _start(conn: sqlite3.Connection, now: float) -> None:
    """Record when learning began and baseline the indexed sources."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE review_control SET activated_at=COALESCE(activated_at,?)",
            (now,),
        )
        # Only chat after this moment is learned.
        activate_sources(conn, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def switch(home: Path, on: bool, now: float) -> Out:
    """Baseline the sources when turning on, then save the choice.

    Args:
        home: Muninn data directory.
        on: True to start learning, False to stop.
        now: Current time in seconds since the epoch.

    Returns:
        The saved choice.
    """
    settings = review_settings.load(home).model_copy(update={"enabled": on})
    if on:
        write_store(home, lambda conn: _start(conn, now))
    review_settings.save(home, settings)
    return {"enabled": on}
