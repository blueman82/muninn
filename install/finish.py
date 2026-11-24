"""Finish a verified install and write its install record."""

from __future__ import annotations

import shutil

from install.constants import INSTALL_RECORD
from install.context import Ctx
from install.record import Record, install_record, save
from install.snapshot import discard
from install.steps_release import sweep


def complete(ctx: Ctx, rec: Record) -> Record:
    """Finish a verified install and write the ``ok`` install record.

    It deletes the release folders that ``prune`` moved aside, keeps each
    store archive, and on an upgrade removes the run record folder.

    Args:
        ctx: The run context.
        rec: The run record.

    Returns:
        The run record, marked complete.

    Raises:
        OSError: If the run record folder cannot be removed or the record
            cannot be written.
    """
    rec["sweep_failed"] = sweep(ctx)
    discard(ctx, rec)
    if ctx.upgrade and not ctx.dry_run:
        shutil.rmtree(ctx.rdir)
        rec["record_removed"] = True
    rec["install_complete"] = True
    if not ctx.dry_run and not rec.get("record_removed"):
        save(ctx, rec)
    install_record(ctx, rec, "ok")
    if ctx.dry_run:
        ctx.say("dry run only: nothing was written")
    else:
        where = (
            ctx.lib / INSTALL_RECORD if rec.get("record_removed") else ctx.rdir
        )
        ctx.say(f"install done; record in {where}")
    return rec
