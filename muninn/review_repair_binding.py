"""Verify a private recovery predecessor across a macOS reboot."""

from __future__ import annotations

from pathlib import Path

from muninn import review_pause_state as state
from muninn import review_volume_identity


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
