"""Publish a verified install and retire superseded recovery records."""

from __future__ import annotations

import shutil

from install import policy_records, review_policy
from install.constants import INSTALL_RECORD
from install.context import Ctx
from install.record import Record, install_record, save
from install.snapshot import discard
from install.steps_release import sweep


def supersede(ctx: Ctx, sha: str) -> None:
    """Retire a completed activation record after a replacement verifies."""
    found = review_policy.pending(ctx)
    if found is None:
        return
    active, rec = found
    if (
        rec["sha"] == sha
        or rec.get("policy_status") != "active"
        or policy_records.activation_phase(rec) != "complete"
        or rec.get("failed", {}).get("step") != "verify"
    ):
        return
    rec["superseded_by"] = sha
    rec.pop("recovery_required", None)
    save(active, rec)


def complete(ctx: Ctx, rec: Record) -> Record:
    """Retain recovery authority and publish the verified final outcome."""
    policy_records.validate(rec)
    # After the last rollback chance, a sweep failure only warns.
    if ctx.upgrade and not ctx.dry_run:
        supersede(ctx, rec["sha"])
    rec["sweep_failed"] = sweep(ctx)
    discard(ctx, rec)
    if (
        ctx.upgrade
        and not ctx.dry_run
        and policy_records.activation_phase(rec) is None
    ):
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
