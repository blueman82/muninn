"""Prepare safe operator guidance for an unresolved repair baseline."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict


class CheckResult(TypedDict):
    """One doctor check as it appears in the JSON output."""

    check: str
    ok: bool | None
    level: str
    detail: str


class RecoveryGuidance(TypedDict):
    """Give safe next steps when a repair baseline needs review."""

    status: str
    platform: str
    reason: str
    steps: list[str]


def recovery_guidance(
    checks: Sequence[CheckResult], platform_name: str
) -> RecoveryGuidance | None:
    """Give safe next steps when a repair baseline is present.

    Args:
        checks: The doctor checks for this run.
        platform_name: The operating-system name from ``sys.platform``.

    Returns:
        Platform guidance, or ``None`` when no repair baseline is present.
    """
    repair = next(
        (check for check in checks if check["check"] == "review_repair"),
        None,
    )
    if repair is None or repair["ok"] is not False:
        return None
    if platform_name == "win32":
        return {
            "status": "manual_recovery_required",
            "platform": platform_name,
            "reason": "windows_repair_not_qualified",
            "steps": [
                "Keep the Muninn data folder and repair files. Do not delete "
                "or rename them.",
                "Windows recovery is not qualified because Muninn cannot "
                "yet verify reboot and repair-file safety on Windows.",
                "Save this report and contact your Muninn maintainer. Do "
                "not restart Muninn until recovery is resolved.",
            ],
        }
    if platform_name not in ("darwin", "linux"):
        return {
            "status": "manual_recovery_required",
            "platform": platform_name,
            "reason": "recovery_platform_unqualified",
            "steps": [
                "Keep the Muninn data folder and repair files. Do not delete "
                "or rename them.",
                "Muninn has no verified repair path for this operating "
                "system.",
                "Save this report and contact your Muninn maintainer.",
            ],
        }
    return {
        "status": "recovery_check_required",
        "platform": platform_name,
        "reason": "repair_baseline_present",
        "steps": [
            "Repair safety is not verified. Doctor only observed a repair "
            "path. Malformed or unsafe records can cause recovery to refuse.",
            "Run `muninn recover`. Muninn will stop its managed writer and "
            "repair the pause only when its safety checks pass.",
            "If it returns `reboot_required`, restart the computer and run "
            "`muninn recover` again.",
            "If it returns `legacy_volume_confirmation_required`, run "
            "`muninn recover --confirm-legacy-volume` only after confirming "
            "that this is the Muninn data you intend to recover.",
            "Run `muninn doctor --pretty` after recovery. If recovery is "
            "refused for another reason, keep all files and send this report "
            "to your Muninn maintainer.",
        ],
    }
