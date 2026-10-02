"""Warning lines the hooks add about the state of the index."""

from __future__ import annotations

from pathlib import Path

from muninn import obs


def index_notes(home: Path) -> tuple[str, ...]:
    """Warning lines about the index: no sign of life, or sources failing.

    Returns:
        A line when the poller shows no sign of life for 3 intervals, and a
        line when the last finished pass could not read some sources; none
        when both are fine.
    """
    status = obs.read_status(home)
    fresh = obs.freshness(status)
    notes: list[str] = []
    if fresh["poller"] != "ok":
        age = fresh["index_age_s"]
        since = "no heartbeat" if age is None else f"last pass {age}s ago"
        notes.append(
            f"muninn: the index is stale ({since});"
            " recent sessions may be missing."
        )
    failed = status.get("failed")
    # Only a real count: status.json is read back from disk.
    if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
        notes.append(
            f"muninn: the last finished pass could not read {failed}"
            " source(s); recent sessions may be missing."
            " run: muninn doctor"
        )
    return tuple(notes)
