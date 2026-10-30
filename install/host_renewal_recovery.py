"""Discover and resume the same strict host grant without inferring consent."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

from install import (
    finish,
    host_renewal,
    host_renewal_resume,
    legacy_state,
    lifecycle,
    policy_preview,
    policy_records,
    review_policy,
)
from install import legacy_baseline as baseline
from install.context import Ctx, StepFailedError
from install.host_renewal import recognize as recognize
from install.host_renewal_models import RenewalReceipt, UpgradeVerified
from install.legacy_state import OperationCheck
from install.record import Record
from muninn import store


def pending(ctx: Ctx) -> tuple[Ctx, Record] | None:
    """Find at most one bounded private incomplete host-renewal envelope."""
    found: list[tuple[Ctx, Record]] = []
    paths = sorted(ctx.rdir.parent.glob("muninn-install-*"))
    if len(paths) > 128:
        raise StepFailedError("host_renewal_limit")
    for directory in paths:
        baseline.directory(directory, private=True)
        path = directory / "rollback-record.json"
        if not os.path.lexists(path):
            code = (
                "host_renewal_record_changed"
                if ctx.renew_codex_host is not None
                else "legacy_record_missing"
            )
            raise StepFailedError(code)
        rec = legacy_state.read(path)
        if "host_renewal" not in rec:
            continue
        receipt = policy_records.host_renewal(rec["host_renewal"])
        active = dataclasses.replace(ctx, ts=receipt.operation)
        if (
            directory != active.rdir
            or rec.get("rdir") != str(directory)
            or (receipt.home, receipt.repo, receipt.sha)
            != (str(ctx.home), rec.get("repo"), rec.get("sha"))
            or not rec.get("upgrade")
            or rec.get("fresh")
        ):
            raise StepFailedError("host_renewal_record_changed")
        if (
            _unfinished(rec, receipt)
            or ctx.renew_codex_host == receipt.plan.digest
        ):
            found.append((active, rec))
    if len(found) > 1:
        raise StepFailedError("host_renewal_recovery_required")
    summary_path = ctx.lib / "install-record.json"
    if os.path.lexists(summary_path):
        summary = legacy_state.read(summary_path)
        if (
            summary.get("host_renewal_phase")
            and summary.get("outcome") != "ok"
            and not found
        ):
            raise StepFailedError("host_renewal_record_changed")
    return found[0] if found else None


def resume_install(
    ctx: Ctx, repo: Path, sha: str, check: OperationCheck
) -> Record | None:
    """Resume only the retained exact operation with its original authority."""
    found = pending(ctx)
    if found is None:
        return None
    active, rec = found
    receipt = policy_records.host_renewal(rec["host_renewal"])
    if (
        ctx.renew_codex_host != receipt.plan.digest
        or (str(repo), sha) != (receipt.repo, receipt.sha)
        or ctx.enable_learning
        or ctx.learning_mode is not None
        or not ctx.upgrade
    ):
        raise StepFailedError("host_renewal_recovery_required")
    review_policy.frozen(ctx, rec, check_supplied=True)
    operation = ctx.host_operation
    if operation is None:
        raise StepFailedError("host_renewal_approval_required")
    active.host_operation = operation
    active.host_plan, active.host_approval = receipt.plan, receipt.approval
    if receipt.held_operation_lock != operation.held:
        host_renewal.phase(
            active, rec, receipt.phase, held_operation_lock=operation.held
        )
    host_renewal.authority(active, rec, check)
    recognized = finish.recognize_failure_summary(active, rec)
    if recognized != receipt.recognized_summary:
        host_renewal.phase(
            active, rec, receipt.phase, recognized_summary=recognized
        )
    host_renewal.authority(active, rec, check)
    if receipt.phase != "upgrade_verified":
        lifecycle.stop(active)
    return rec


def finish_verified(ctx: Ctx, rec: Record, check: OperationCheck) -> Record:
    """Finish retained bookkeeping without claiming another health check."""
    receipt = host_renewal.authority(ctx, rec, check)
    if not isinstance(receipt, UpgradeVerified):
        raise StepFailedError("host_renewal_recovery_required")
    checked = host_renewal_resume.capture(ctx, rec, receipt)
    path = store.db_path(ctx.data)
    with policy_preview.connection(path) as conn:
        conn.row_factory = sqlite3.Row
        host_renewal_resume.guarded(ctx, rec, receipt, checked, path, conn)
        host_renewal.authority(ctx, rec, check)
        if finish.publication_complete(ctx, rec):
            return rec
        prospective: Record = rec | {"install_complete": True}
        policy_records.check_renewal_size(prospective)
        try:
            return finish.complete(ctx, rec)
        except Exception:
            ctx.say("host approval committed; upgrade incomplete")
            raise


def _unfinished(rec: Record, receipt: RenewalReceipt) -> bool:
    """Distinguish completed publication from a retained verified receipt."""
    if "install_complete" in rec and type(rec["install_complete"]) is not bool:
        raise StepFailedError("host_renewal_record_changed")
    return (
        receipt.phase != "upgrade_verified"
        or rec.get("install_complete") is not True
    )
