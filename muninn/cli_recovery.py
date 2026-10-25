"""Recover interrupted maintenance after the platform proves it is safe."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from muninn import cli_core, review_pause, store
from muninn import review_pause_state as state
from muninn.cli_core import Env, Record, Result

__all__ = ["recover"]


def _platform_name() -> str:
    """Return the platform used to select qualified recovery behavior."""
    return sys.platform


def _needs_recovery(home: Path) -> tuple[bool, bool]:
    """Check whether private pause records show unfinished maintenance.

    Args:
        home: Muninn's private data directory.

    Returns:
        Whether recovery is needed, and whether a repair baseline exists.

    Raises:
        ValueError: If a protocol record is unsafe or malformed.
        review_pause.PauseRefusedError: If a baseline is not verified.
    """
    witness_raw, witness_file = state.inspect(home, state.WITNESS)
    baseline_raw, baseline_file = state.inspect(home, state.BASELINE)
    witness = state.parse(witness_raw, state.Witness)
    baseline = state.parse(baseline_raw, state.Baseline)
    if baseline_file is not None and baseline is None:
        raise review_pause.PauseRefusedError("pause_unverified")
    if witness_file is not None and witness is None:
        return True, baseline_file is not None
    if baseline_file is not None:
        return True, True
    _, lock_file = state.inspect(home, state.LOCK)
    if (
        witness_file is None
        and baseline_file is None
        and lock_file is None
        and not store.db_path(home).exists()
    ):
        return False, False
    if (
        witness is not None
        and witness.state == "complete"
        and lock_file is not None
        and witness.lock_identity == lock_file.identity
    ):
        return False, False
    return True, False


_WHY = {
    "pause_unverified": (
        "The saved pause records do not prove that the same Muninn files "
        "and process are present."
    ),
    "windows_repair_unqualified": (
        "Muninn has not qualified Windows boot checks and repair-file "
        "retirement, so it cannot safely change this state."
    ),
    "recovery_platform_unqualified": (
        "Muninn has no verified repair path for this operating system."
    ),
    "reboot_required": (
        "Muninn cannot prove that the interrupted process is gone until "
        "the computer has rebooted."
    ),
    "coordination_timeout": (
        "The managed Muninn writer did not release its lock before the "
        "wait limit."
    ),
    "boot_identity_unavailable": (
        "The operating system did not provide a boot identity Muninn can "
        "verify."
    ),
    "owner_changed": (
        "The process that owns the maintenance pause changed during "
        "recovery."
    ),
}


def _refusal(reason: str, next_action: str) -> Result:
    """Return a stable reason and one plain next action."""
    return 2, {
        "error": "refused",
        "reason": reason,
        "why": _WHY.get(reason, "Muninn could not verify recovery is safe."),
        "next_action": next_action,
    }


def recover(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run only the existing checked recovery path; never erase user data.

    Args:
        a: Parsed command arguments (unused).
        env: Process environment (unused).
        home: Muninn's private data directory.
        record: Content-free call-log entry.

    Returns:
        A JSON-ready recovery result and exit code.
    """
    del a, env
    if not home.exists():
        return _no_recovery()
    try:
        needed, has_baseline = _needs_recovery(home)
    except (OSError, ValueError):
        return _refusal(
            "pause_unverified",
            "Keep all files unchanged. Save `muninn --pretty doctor` and "
            "contact your Muninn maintainer.",
        )
    if not needed:
        return _no_recovery()
    platform_name = _platform_name()
    refusal = _platform_refusal(home, platform_name, has_baseline)
    if refusal is not None:
        return refusal
    return _repair(home, platform_name, record)


def _no_recovery() -> Result:
    """Return the common answer when no unfinished pause exists.

    Returns:
        A successful no-recovery result.
    """
    return 0, {"recovery": {"status": "no_recovery_needed"}}


def _platform_refusal(
    home: Path, platform_name: str, has_baseline: bool
) -> Result | None:
    """Refuse platforms or baselines without a verified repair path.

    Args:
        home: Muninn's private data directory.
        platform_name: Current operating-system identifier.
        has_baseline: Whether a saved boot baseline needs validation.

    Returns:
        A refusal result, or None when repair can proceed.
    """
    if platform_name == "win32":
        return _refusal(
            "windows_repair_unqualified",
            "Keep all files unchanged. Save `muninn --pretty doctor` and "
            "contact your Muninn maintainer.",
        )
    if platform_name not in ("darwin", "linux"):
        return _refusal(
            "recovery_platform_unqualified",
            "Keep all files unchanged and contact your Muninn maintainer.",
        )
    if has_baseline:
        try:
            matches_platform = _baseline_matches_platform(home, platform_name)
        except (OSError, ValueError):
            matches_platform = False
        if not matches_platform:
            return _refusal(
                "pause_unverified",
                "Keep all files unchanged. Save `muninn --pretty doctor` and "
                "contact your Muninn maintainer.",
            )
    return None


def _repair(home: Path, platform_name: str, record: Record) -> Result:
    """Repair through the existing checked maintenance barriers.

    Args:
        home: Muninn's private data directory.
        platform_name: Current operating-system identifier.
        record: Content-free call-log entry.

    Returns:
        A recovery result or a refusal with its next action.
    """
    status = review_pause.EraseStatus()
    try:
        with review_pause.erase_connection(
            home, wait_s=cli_core.WRITER_WAIT_S, status=status
        ):
            pass
    except review_pause.PauseRefusedError as error:
        next_action = {
            "reboot_required": (
                "Restart the computer, then run `muninn recover` again."
            ),
            "coordination_timeout": (
                "Wait for Muninn to become idle, then run `muninn recover` "
                "again."
            ),
        }.get(
            error.code,
            "Keep all files unchanged. Save `muninn --pretty doctor` and "
            "contact your Muninn maintainer.",
        )
        return _refusal(error.code, next_action)
    del record
    return 0, {
        "recovery": {
            "status": (
                "recovered"
                if status.repair_phase == "complete"
                else "no_recovery_needed"
            ),
            "message": (
                "Muninn repaired the interrupted pause using saved identity "
                "checks and applied any recorded erasure intent."
                if status.repair_phase == "complete"
                else "No interrupted maintenance pause needed repair."
            ),
            "next_action": (
                "Run `muninn --pretty doctor` to check the service. If it is "
                "stopped, start it through your system service manager."
                if status.repair_phase == "complete"
                else "No further recovery action is needed."
            ),
            "repair_phase": status.repair_phase,
            "platform": platform_name,
        }
    }


def _baseline_matches_platform(home: Path, platform_name: str) -> bool:
    """Refuse a baseline whose recorded operating system differs today."""
    raw, _ = state.inspect(home, state.BASELINE)
    baseline = state.parse(raw, state.Baseline)
    return baseline is not None and baseline.boot.startswith(
        platform_name + ":"
    )
