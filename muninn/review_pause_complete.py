"""Verify completed owners and migrate confirmed legacy disk identities."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from muninn import review_boot_identity, review_volume_identity
from muninn import review_pause_state as state
from muninn.review_repair_binding import (
    RepairSnapshot,
    legacy_completed_candidate,
    same_repair_binding,
)


class RepairStatus(Protocol):
    """Retain the recovery milestone through publication errors."""

    bind_volume: bool
    repair_phase: Literal[
        "none", "baseline_recorded", "reconciling", "complete"
    ]


def verify(
    home: Path,
    fd: int,
    snapshot: RepairSnapshot,
    *,
    repair: bool,
    confirmed: bool,
    status: RepairStatus,
    reconcile: Callable[[], object],
) -> bool:
    """Replay current intent only after completion and identity checks.

    Args:
        home: Private Muninn home beneath the retained maintenance locks.
        fd: Held lifetime lock descriptor.
        snapshot: Exact private records inspected beneath both locks.
        repair: Whether this caller permits record repair.
        confirmed: Whether the owner confirmed legacy disk identity.
        status: Caller-owned milestone and runtime format capability.
        reconcile: Replay current intent or check completed historical erasure.

    Returns:
        True for verified completion, or False for an uncertain owner.

    Raises:
        PauseRefusedError: If identity, confirmation or records fail.
    """
    witness = snapshot.witness
    if witness is None:
        return False
    matched = state.completed_matches(home, witness, snapshot.identity)
    migrating = not matched and legacy_completed_candidate(
        witness, snapshot.identity
    )
    if not matched and not migrating:
        return False
    volume = witness.volume_uuid
    if migrating:
        volume = _require_migration(
            home, snapshot, repair=repair, confirmed=confirmed
        )
    elif snapshot.baseline_file is not None and not repair:
        raise state.PauseRefusedError("pause_unverified")
    if migrating:
        status.repair_phase = "reconciling"
    reconcile()
    if (
        state.inspect(home, state.WITNESS)[1] != snapshot.predecessor
        or (state.inspect(home, state.BASELINE)[1] != snapshot.baseline_file)
        or (
            volume is not None
            and review_volume_identity.observe(home) != volume
        )
    ):
        raise state.PauseRefusedError("pause_unverified")
    if migrating:
        updated = state.make_witness(home, fd, bind_volume=status.bind_volume)
        if status.bind_volume and updated.volume_uuid != volume:
            raise state.PauseRefusedError("pause_unverified")
        state.publish(home, fd, state.WITNESS, updated)
    state.retire_baseline(home, fd)
    if migrating or snapshot.baseline_file is not None:
        status.repair_phase = "complete"
    return True


def _require_migration(
    home: Path, snapshot: RepairSnapshot, *, repair: bool, confirmed: bool
) -> str:
    """Require exact captured files and explicit legacy disk confirmation.

    Returns:
        The native disk UUID checked before replay.

    Raises:
        PauseRefusedError: If repair, binding or confirmation is absent.
    """
    if not repair:
        raise state.PauseRefusedError("pause_unverified")
    if snapshot.baseline_file is not None and (
        snapshot.baseline is None
        or not same_repair_binding(
            home,
            snapshot.baseline,
            snapshot.identity,
            snapshot.predecessor,
            review_boot_identity.observe(),
        )
    ):
        raise state.PauseRefusedError("pause_unverified")
    volume = review_volume_identity.observe(home)
    if not confirmed:
        raise state.PauseRefusedError("legacy_volume_confirmation_required")
    return volume
