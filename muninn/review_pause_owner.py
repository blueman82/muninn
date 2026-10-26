"""Poll lifetime ownership without accepting an unstable witness read."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from muninn import poller_stop
from muninn import review_pause_state as state
from muninn.review_pause_state import PauseRefusedError

POLL_SECONDS = 0.1


class StopStatus(Protocol):
    """Retain possible stop publication through an original caller error."""

    stop_may_have_been_published: bool


def latest(home: Path) -> state.Witness | None:
    """Read a safe bounded witness without treating a routing hint as proof."""
    return state.parse(state.inspect(home, state.WITNESS)[0], state.Witness)


def same_owner(
    selected: state.Witness | None, current: state.Witness | None
) -> None:
    """Refuse any observed replacement of the one selected stop target."""
    if selected is not None and (
        current is None
        or current.owner != selected.owner
        or current.lock_identity != selected.lock_identity
    ):
        raise PauseRefusedError("owner_changed")


def acquire(
    home: Path, deadline: float, status: StopStatus
) -> tuple[int, state.Witness | None]:
    """Acquire lifetime ownership while targeting at most one active owner.

    Args:
        home: Existing private store directory.
        deadline: Original monotonic acquisition deadline.
        status: Caller-owned stop-publication milestone.

    Returns:
        The acquired descriptor and any previously selected stop target.

    Raises:
        OSError: If a read fails for a reason other than identity replacement.
        PauseRefusedError: If the deadline or selected-owner check fails.
    """
    selected = None
    while True:
        try:
            return state.acquire(home), selected
        except BlockingIOError:
            try:
                current = latest(home)
            except OSError as error:
                # Completion replaces the witness before lifetime lock release.
                # Retry only this opener refusal, never a failed safety check.
                if type(error) is not OSError or error.args != (
                    "source changed identity",
                ):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PauseRefusedError("coordination_timeout") from error
                time.sleep(min(POLL_SECONDS, remaining))
                continue
            same_owner(selected, current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PauseRefusedError("coordination_timeout") from None
            if (
                selected is None
                and current is not None
                and current.state == "active"
            ):
                assert current.owner is not None
                lock = (home / state.LOCK).stat()
                if current.lock_identity != (lock.st_dev, lock.st_ino):
                    raise PauseRefusedError("pause_unverified") from None
                selected = current
                status.stop_may_have_been_published = True
                poller_stop.request(
                    home,
                    current.owner.pid,
                    current.owner.generation,
                    deadline=deadline,
                )
                continue
            time.sleep(min(POLL_SECONDS, remaining))
