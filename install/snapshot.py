"""Retain private store archives without restoring over current data."""

from __future__ import annotations

import functools

from install.context import Ctx, StepFailedError, job, must, wait
from install.cutover import prepare_store
from install.record import Record


def discard(ctx: Ctx, rec: Record) -> bool:
    """Retain every recorded recoverable archive after a successful upgrade.

    Args:
        ctx: Installer context, including dry-run mode.
        rec: Existing run record naming any retained source archive.

    Returns:
        True; retention does not remove the recorded file.
    """
    snap = rec.get("snapshot")
    if snap and not ctx.dry_run:
        snap["state"] = "retained"
    return True


def _running(ctx: Ctx) -> bool:
    """Say whether the launchd job has a live process."""
    found = job(ctx)
    return bool(found and found["pid"])


def undo_store(ctx: Ctx, rec: Record) -> bool:
    """Refuse blind restoration of an archive over potentially newer state.

    Args:
        ctx: Installer context identifying the current data directory.
        rec: Existing rollback record naming a recoverable archive.

    Returns:
        False if no archive requires a recovery decision.

    Raises:
        StepFailedError: If a retained archive needs explicit review.
    """
    snap = rec.get("snapshot")
    if snap and (ctx.data / snap["name"]).exists():
        raise StepFailedError("archive_restore_requires_review")
    return False


def start_again(ctx: Ctx) -> None:
    """Load the job again on the relinked release after a restore."""
    must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    wait(ctx, functools.partial(_running, ctx), 30, "job has no live PID")


def snapshot_store(ctx: Ctx, rec: Record) -> None:
    """Prepare the governed store without numeric-version migration guesses."""
    prepare_store(ctx, rec)
