"""Turn learning on or off for the serve process."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muninn import review_settings
from muninn.cli_core import Out, write_store
from muninn.review_activation import activate_sources
from muninn.review_json import canonical_json
from muninn.review_progress import checked_policy, digest


def _set(
    conn: sqlite3.Connection,
    settings: review_settings.Settings,
    now: float,
) -> None:
    """Install the policy and open admission in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if settings.enabled:
            policy = checked_policy(review_settings.policy_for(settings))
            conn.execute(
                "UPDATE review_control SET admission=1,policy_json=?,"
                "policy_digest=?,activated_at=COALESCE(activated_at,?)",
                (canonical_json(policy), digest(policy), now),
            )
            # Only chat after this moment is learned.
            activate_sources(conn, now)
        else:
            conn.execute("UPDATE review_control SET admission=0")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def switch(home: Path, on: bool, now: float) -> Out:
    """Save the choice, then change admission to match.

    Args:
        home: Muninn data directory.
        on: True to start learning, False to stop.
        now: Current time in seconds since the epoch.

    Returns:
        The saved choice.
    """
    settings = review_settings.load(home).model_copy(update={"enabled": on})
    write_store(home, lambda conn: _set(conn, settings, now))
    review_settings.save(home, settings)
    return {"enabled": on}
