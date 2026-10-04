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
    failed = obs.count_field(status, "failed")
    if failed:
        notes.append(
            f"muninn: the last finished pass could not read {failed}"
            " source(s); recent sessions may be missing."
            " run: muninn doctor"
        )
    unreadable = obs.count_field(status, "unreadable_files")
    if unreadable:
        notes.append(
            f"muninn: the last pass could not open {unreadable} transcript"
            " file(s); check file permissions. run: muninn doctor"
        )
    err = obs.poller_error(status)
    if err and _failed_since_pass(status):
        notes.append(
            f"muninn: the last pass stopped with {err};"
            " recent sessions may be missing. run: muninn doctor"
        )
    return tuple(notes)


def _failed_since_pass(status: dict[str, object]) -> bool:
    """Whether an error was recorded after the last good pass.

    A good pass clears ``last_error``, so this only guards a stale or
    hand-edited file where the error stamp is older than the pass stamp.

    Args:
        status: The parsed ``status.json`` contents.

    Returns:
        True when the last error is newer than, or cannot be ordered
        against, the last good pass.
    """
    at, ok = status.get("last_error_at"), status.get("last_pass_at")
    if not isinstance(at, int | float) or isinstance(at, bool):
        return True
    return not isinstance(ok, int | float) or at >= ok
