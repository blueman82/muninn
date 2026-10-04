"""Derived expiry and restriction rules of the knowledge ledger.

Expiry is computed at read time and never written: an entry whose
``valid_until`` has passed keeps its status and its row, and readers simply
stop treating it as current.  A restricted entry is shown to the person at
the CLI but is never pushed into a prompt.  Both rules live here so every
query that decides "current" or "pushable" shares one definition.
"""

from __future__ import annotations

import time

# One bound parameter: the current epoch seconds.  Alias ``k`` is knowledge.
LIVE_SQL = "(k.valid_until IS NULL OR k.valid_until > ?)"
# Rows that may be pushed: live and not restricted (one bound parameter).
PUSH_SQL = f"{LIVE_SQL} AND k.sensitivity = 'normal'"


def is_expired(status: str, valid_until: float | None, now: float) -> bool:
    """Return whether a current entry's expiry has passed.

    Args:
        status: Stored status; only ``current`` entries can be expired.
        valid_until: Stored expiry in epoch seconds, or None for never.
        now: Current epoch seconds.

    Returns:
        True for a current entry with ``valid_until`` at or before ``now``.
    """
    return (
        status == "current" and valid_until is not None and valid_until <= now
    )


def iso_stamp(at: float | None) -> str | None:
    """Return a UTC ISO 8601 timestamp, or None for no timestamp."""
    if at is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
