"""Find safe install-recovery actions and print them for the user."""

from __future__ import annotations

import shlex
import sqlite3
import sys
from argparse import Namespace
from contextlib import closing
from typing import NoReturn, cast

from install import decision_source, policy_preview, snapshot_io
from install.context import Ctx, StepFailedError, dry
from install.policy_source_format import legacy_source, validate_policy_source
from install.record import Record
from install.steps_release import installed_env
from muninn import cli_recovery, store
from muninn import review_pause as q


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
    if _historical_checked(ctx, rec):
        return
    try:
        code, result = cli_recovery.recover(
            Namespace(), installed_env(ctx), ctx.data, {}
        )
        if _reason(result) == "legacy_volume_confirmation_required":
            approved = _confirm_legacy_volume(ctx)
            if approved:
                code, result = cli_recovery.recover(
                    Namespace(confirm_legacy_volume=True),
                    installed_env(ctx),
                    ctx.data,
                    {},
                )
            elif approved is None:
                result = {
                    "reason": "legacy_volume_confirmation_required",
                    "why": (
                        "The saved file matches, but the older note has no "
                        "disk ID."
                    ),
                    "next_action": (
                        "Run this same install command in a terminal to "
                        "confirm recovery."
                    ),
                }
            else:
                result = {
                    "reason": "legacy_volume_confirmation_declined",
                    "why": "You declined. No files were changed.",
                }
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


def _historical_checked(ctx: Ctx, rec: Record) -> bool:
    """Explain historical repair without invoking a current-only recovery."""
    try:
        return _historical_store(ctx)
    except StepFailedError as exc:
        if str(exc) != "marked_v1_recovery_required":
            raise
        rec["recovery_required"] = True
        ctx.say(
            "The marked-v1 pause or erasure needs its verified installed "
            "recovery."
        )
        ctx.say(
            "  Use the verified v1 installed muninn recover command, then "
            "retry this upgrade."
        )
        ctx.say(
            "  Keep the old release and all data unchanged until recovery "
            "succeeds."
        )
        raise


def _reason(result: object) -> str:
    """Return one fixed recovery reason from a CLI result."""
    if not isinstance(result, dict):
        return ""
    reason = cast(dict[str, object], result).get("reason")
    return reason if isinstance(reason, str) else ""


def _confirm_legacy_volume(ctx: Ctx) -> bool | None:
    """Ask once after the read-only checks match an older macOS record.

    Returns:
        True when the owner approves, False when they decline, or None when
        the installer has no terminal.
    """
    if not sys.stdin.isatty():
        return None
    ctx.say("Muninn found an older recovery note without a disk ID.")
    ctx.say("The saved file matches, but Muninn cannot verify the same disk.")
    ctx.say(
        "Continuing applies saved deletion requests, then continues "
        "the upgrade."
    )
    try:
        answer = input("Continue with this Muninn data? [y/N] ")
    except EOFError:
        return False
    return answer.strip().casefold() in ("y", "yes")


def _historical_store(ctx: Ctx) -> bool:
    """Leave supported historical stores for explicit installer migration."""
    path = store.db_path(ctx.data)
    if not path.is_file():
        return False
    if legacy_source(path):
        return True
    with policy_preview.connection(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            return False
        try:
            validate_policy_source(conn)
        except StepFailedError as exc:
            raise StepFailedError("cutover_source_mismatch") from exc
    with (
        q.maintenance_owner(
            ctx.data, wait_s=15.0, status=q.EraseStatus()
        ) as owner,
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as check,
        closing(
            sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, isolation_level=None
            )
        ) as conn,
    ):
        check()
        decision_source.verify_source(owner, conn)
        check()
    return True


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
    elif reason == "legacy_volume_confirmation_required":
        ctx.say(f"  Next: {action}")
        ctx.say("  No recovery or upgrade changes were made.")
    elif reason == "legacy_volume_confirmation_declined":
        ctx.say("  No recovery or upgrade changes were made.")
        ctx.say("  Run this install command again when you want to continue.")
    elif action:
        ctx.say(f"  Next: {action}")
        ctx.say("  After recovery, rerun this same install command.")
    else:
        ctx.say(
            "  Keep all files unchanged and contact your Muninn maintainer."
        )
    ctx.say(f"  Health report: {health_report_command(ctx)}")
    raise StepFailedError("maintenance_recovery_required")
