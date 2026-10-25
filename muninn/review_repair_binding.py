"""Verify a private recovery predecessor across a macOS reboot."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from muninn import review_boot_identity, review_volume_identity
from muninn import review_pause_state as state


@dataclass(frozen=True)
class RepairSnapshot:
    """Hold the evidence read again under the maintenance lock."""

    baseline: state.Baseline | None
    baseline_file: state.Fingerprint | None
    witness: state.Witness | None
    identity: state.Identity
    predecessor: state.Fingerprint | None
    boot: str


def legacy_volume_change_requires_confirmation(home: Path) -> bool:
    """Check whether an old Darwin record needs owner confirmation."""
    if sys.platform != "darwin":
        return False
    baseline_raw, _ = state.inspect(home, state.BASELINE)
    baseline = state.parse(baseline_raw, state.Baseline)
    if baseline is None or not baseline.boot.startswith("darwin:"):
        return False
    witness_raw, witness_file = state.inspect(home, state.WITNESS)
    _, lock_file = state.inspect(home, state.LOCK)
    witness = state.parse(witness_raw, state.Witness)
    if witness_file is None or lock_file is None:
        return False
    if not legacy_device_change_matches(
        baseline,
        witness,
        lock_file.identity,
        witness_file,
        review_boot_identity.observe(),
    ):
        return False
    review_volume_identity.observe(home)
    return True


def legacy_device_change_matches(
    baseline: state.Baseline,
    witness: state.Witness | None,
    identity: state.Identity,
    predecessor: state.Fingerprint | None,
    boot: str,
) -> bool:
    """Match an old Darwin record by unchanged inodes and witness bytes."""
    before = baseline.predecessor
    return (
        baseline.volume_uuid is None
        and baseline.boot.startswith("darwin:")
        and boot.startswith("darwin:")
        and baseline.boot != boot
        and witness is not None
        and witness.state == "active"
        and witness.lock_identity[1] == identity[1]
        and before is not None
        and predecessor is not None
        and baseline.lock_identity[1] == identity[1]
        and before.identity[1] == predecessor.identity[1]
        and before.sha256 == predecessor.sha256
        and (
            baseline.lock_identity[0] != identity[0]
            or before.identity[0] != predecessor.identity[0]
        )
    )


def rebind_legacy_volume(
    home: Path,
    fd: int,
    snapshot: RepairSnapshot,
    confirmed: bool,
) -> tuple[state.Baseline | None, state.Fingerprint | None]:
    """Apply one approved disk binding after repeating all legacy checks."""
    baseline = snapshot.baseline
    if (
        baseline is None
        or not baseline.boot.startswith("darwin:")
        or baseline.volume_uuid is not None
        or not (
            baseline.boot.split(":")[0] != snapshot.boot.split(":")[0]
            or baseline.lock_identity != snapshot.identity
            or baseline.predecessor != snapshot.predecessor
        )
    ):
        return baseline, snapshot.baseline_file
    if not legacy_device_change_matches(
        baseline,
        snapshot.witness,
        snapshot.identity,
        snapshot.predecessor,
        snapshot.boot,
    ):
        raise state.PauseRefusedError("pause_unverified")
    if not confirmed:
        raise state.PauseRefusedError("legacy_volume_confirmation_required")
    volume_uuid = review_volume_identity.observe(home)
    state.publish(
        home,
        fd,
        state.BASELINE,
        state.Baseline(
            version=baseline.version,
            lock_identity=snapshot.identity,
            boot=baseline.boot,
            predecessor=snapshot.predecessor,
            volume_uuid=volume_uuid,
        ),
    )
    raw, saved_file = state.inspect(home, state.BASELINE)
    updated = state.parse(raw, state.Baseline)
    if updated is None or saved_file is None:
        raise state.PauseRefusedError("pause_unverified")
    return updated, saved_file


def same_repair_binding(
    home: Path,
    baseline: state.Baseline,
    identity: state.Identity,
    predecessor: state.Fingerprint | None,
    boot: str,
) -> bool:
    """Match an exact predecessor or a proven macOS volume renumbering."""
    if (
        baseline.lock_identity == identity
        and baseline.predecessor == predecessor
    ):
        if boot.startswith("darwin:") and baseline.volume_uuid is not None:
            return review_volume_identity.observe(home) == baseline.volume_uuid
        return True
    before = baseline.predecessor
    if (
        baseline.boot == boot
        or not boot.startswith("darwin:")
        or before is None
        or predecessor is None
        or baseline.volume_uuid is None
    ):
        return False
    old_device, old_inode = baseline.lock_identity
    current_device, current_inode = identity
    return (
        old_device != current_device
        and old_inode == current_inode
        and before.identity[0] == old_device
        and predecessor.identity[0] == current_device
        and before.identity[1] == predecessor.identity[1]
        and before.sha256 == predecessor.sha256
        and review_volume_identity.observe(home) == baseline.volume_uuid
    )
