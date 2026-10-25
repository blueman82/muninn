"""Find safe install-recovery actions and print them for the user."""

from __future__ import annotations

import shlex
from argparse import Namespace
from typing import NoReturn, cast

from install.context import Ctx, StepFailedError, dry
from install.record import Record
from install.steps_release import installed_env
from muninn import cli_recovery


def health_report_command(ctx: Ctx) -> str:
    """Format a copyable health-report command for the active shell.

    Args:
        ctx: Installer paths and target platform.

    Returns:
        A command that runs doctor from the installed executable.
    """
    if ctx.platform == "win32":
        executable = str(ctx.muninn).replace("'", "''")
        return f"& '{executable}' --pretty doctor"
    return f"{shlex.quote(str(ctx.muninn))} --pretty doctor"


def recover_pause(ctx: Ctx, rec: Record) -> None:
    """Repair a saved maintenance pause before an upgrade changes the store.

    Args:
        ctx: Installer paths and output context.
        rec: The install record, marked when recovery remains unresolved.

    Raises:
        StepFailedError: If recovery needs a person or cannot prove it is safe.
    """
    if dry(ctx, "would check for and repair an interrupted maintenance pause"):
        return
    _run_pause_recovery(ctx, rec)


def _run_pause_recovery(ctx: Ctx, rec: Record) -> None:
    """Run the checked repair and report any human step.

    Args:
        ctx: Installer paths and output context.
        rec: Install record marked when recovery remains unresolved.

    Raises:
        StepFailedError: If repair fails or needs a person.
    """
    try:
        code, result = cli_recovery.recover(
            Namespace(), installed_env(ctx), ctx.data, {}
        )
    except Exception as exc:
        rec["recovery_required"] = True
        ctx.say("Muninn could not check whether this pause is safe to repair.")
        ctx.say(
            "  Keep all files unchanged and contact your Muninn maintainer."
        )
        ctx.say(f"  Health report: {health_report_command(ctx)}")
        raise StepFailedError("maintenance_recovery_required") from exc
    if result is None:
        _fail_pause_recovery(ctx, rec, "", "")
    recovery_value = result.get("recovery")
    recovery = (
        cast(dict[str, object], recovery_value)
        if isinstance(recovery_value, dict)
        else {}
    )
    if code == 0 and recovery.get("status") in (
        "recovered",
        "no_recovery_needed",
    ):
        if recovery.get("status") == "recovered":
            ctx.say("Muninn repaired the pause. The upgrade will continue.")
        return
    reason = result.get("reason")
    action = result.get("next_action")
    why = result.get("why")
    _fail_pause_recovery(
        ctx,
        rec,
        reason if isinstance(reason, str) else "",
        action if isinstance(action, str) else "",
        why if isinstance(why, str) else "",
    )


def _fail_pause_recovery(
    ctx: Ctx, rec: Record, reason: str, action: str, why: str = ""
) -> NoReturn:
    """Stop the upgrade and explain how to continue safely.

    Args:
        ctx: Installer paths and output context.
        rec: Install record marked as requiring recovery.
        reason: Fixed recovery refusal code.
        action: Safe command or action returned by recovery.
        why: Plain-language reason returned by recovery.

    Raises:
        StepFailedError: Always, because recovery did not complete.
    """
    rec["recovery_required"] = True
    ctx.say("Muninn could not repair the pause safely.")
    if why:
        ctx.say(f"  {why}")
    if reason == "reboot_required":
        ctx.say(
            "  Restart the computer, then rerun this same install command. "
            "Muninn will retry recovery automatically."
        )
    elif action:
        ctx.say(f"  Next: {action}")
        ctx.say("  After recovery, rerun this same install command.")
    else:
        ctx.say(
            "  Keep all files unchanged and contact your Muninn maintainer."
        )
    ctx.say(f"  Health report: {health_report_command(ctx)}")
    raise StepFailedError("maintenance_recovery_required")
