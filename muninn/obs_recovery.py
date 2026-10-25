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
    reason = {
        "linux": "repair_baseline_present",
        "win32": "windows_repair_not_qualified",
    }.get(platform_name, "repair_baseline_present")
    return {
        "status": "manual_recovery_required",
        "platform": platform_name,
        "reason": reason,
        "steps": [
            "Check the service status in this report.",
            "If Muninn is stopped, leave it stopped until recovery is "
            "complete.",
            "Keep the data folder and all repair files. Do not delete or "
            "rename them.",
            "Save this report and contact your Muninn maintainer before "
            "restart.",
        ],
    }
