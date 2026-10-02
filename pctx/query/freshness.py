"""Index freshness fields derived from the poller's status.json."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

# Larger ages are reported as unknown, which keeps the metadata bounded.
MAX_INDEX_AGE = 2**63 - 1


def _unknown() -> dict[str, Any]:
    """Return the fields for an index whose age cannot be established."""
    return {"index_age_s": None, "poller": "stale"}


def freshness(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return index_age_s and poller ok|stale from a parsed status.json.

    Args:
        status: Parsed status.json, or None when the caller has none.

    Returns:
        An empty dict without a status; otherwise the two fields, with the
        poller "stale" whenever the age is unknown or over three intervals.
    """
    if status is None:
        return {}
    last = status.get("last_pass_at")
    if not isinstance(last, (int, float)):
        return _unknown()
    every = status.get("interval_s")
    every = every if isinstance(every, (int, float)) and every > 0 else 60
    try:
        age = max(0, int(time.time() - last))
    except (OverflowError, ValueError):  # inf or nan in a hand-edited file
        return _unknown()
    if age > MAX_INDEX_AGE:
        return _unknown()
    return {
        "index_age_s": age,
        "poller": "ok" if age <= 3 * every else "stale",
    }
