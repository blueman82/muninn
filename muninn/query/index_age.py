"""Index freshness fields derived from the poller's status.json."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

# Larger ages are reported as unknown, which keeps the metadata bounded.
MAX_INDEX_AGE = 2**63 - 1
# status.json key the poller refreshes while a long pass runs, so a pass that
# outlasts the heartbeat window does not read as a dead poller.
ALIVE_AT = "alive_at"


def _since(stamp: object) -> int | None:
    """Return whole seconds since ``stamp``, or None if it is unusable."""
    if not isinstance(stamp, (int, float)):
        return None
    try:
        return max(0, int(time.time() - stamp))
    except (OverflowError, ValueError):  # inf or nan in a hand-edited file
        return None


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
    age = _since(status.get("last_pass_at"))
    if age is None or age > MAX_INDEX_AGE:
        return _unknown()
    every = status.get("interval_s")
    every = every if isinstance(every, (int, float)) and every > 0 else 60
    # The index is as old as its last finished pass, but the poller is alive
    # if it either finished a pass or said so recently.
    alive = _since(status.get(ALIVE_AT))
    seen = age if alive is None else min(age, alive)
    return {
        "index_age_s": age,
        "poller": "ok" if seen <= 3 * every else "stale",
    }
