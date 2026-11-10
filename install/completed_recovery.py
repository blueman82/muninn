"""Repair only completed historical owners with already satisfied erasure."""

from __future__ import annotations

import sqlite3

from install import decision_source
from install.context import StepFailedError
from muninn import review_pause as q
from muninn import review_pause_complete, review_pause_owner
from muninn import review_pause_state as state
from muninn.review_repair_binding import (
    RepairSnapshot,
    legacy_completed_candidate,
)


def repair(
    owner: q.MaintenanceOwner, conn: sqlite3.Connection, *, confirmed: bool
) -> None:
    """Rebind a confirmed legacy completion without writing a historical DB.

    Args:
        owner: Caller-held lifetime lock and active writer barrier.
        conn: Caller-owned read-only exact marked-one source.
        confirmed: Explicit approval for the absent original disk binding.

    Raises:
        StepFailedError: If historical erasure or completion is unresolved.
        PauseRefusedError: If identity, baseline or confirmation is absent.
    """
    decision_source.verify_erasure(owner, conn)
    home = owner.home
    identity = state.lock_identity(home, owner.fd)
    raw, predecessor = state.inspect(home, state.WITNESS)
    witness = state.parse(raw, state.Witness)
    review_pause_owner.same_owner(owner.selected, witness)
    if not legacy_completed_candidate(witness, identity):
        raise StepFailedError("marked_v1_recovery_required")
    baseline_raw, baseline_file = state.inspect(home, state.BASELINE)
    snapshot = RepairSnapshot(
        state.parse(baseline_raw, state.Baseline),
        baseline_file,
        witness,
        identity,
        predecessor,
        "",
    )
    if not review_pause_complete.verify(
        home,
        owner.fd,
        snapshot,
        repair=True,
        confirmed=confirmed,
        status=owner.status,
        reconcile=lambda: decision_source.verify_erasure(owner, conn),
    ):
        raise StepFailedError("marked_v1_recovery_required")
