"""Stage managed legacy exclusion and validate a later supported boot."""

from __future__ import annotations

from pathlib import Path

from install import legacy_baseline as baseline
from install import legacy_exclusion as exclusion
from install import legacy_recovery, review_policy
from install import legacy_state as state
from install.context import Ctx, StepFailedError, link_text
from install.cutover import install_legacy, resume_policy
from install.legacy_state import boot as boot
from install.legacy_state import retained as _retained
from install.legacy_state import validate_boot as validate_boot
from install.preflight import preflight, validate_resume_repo
from install.record import Record
from install.steps_release import prepare_release
from muninn import file_sync
from muninn import review_pause as q
from muninn.platform_paths import read_selection


def selected(ctx: Ctx) -> bool:
    """Recognize only the exact pinned legacy selection for this route."""
    if ctx.platform == "win32":
        found = read_selection(ctx.lib.parent)
        return found is not None and found[0].name == baseline.BASELINE
    path = ctx.lib / "current"
    return path.is_symlink() and link_text(path) == baseline.BASELINE


def _claim(ctx: Ctx, rec: Record) -> None:
    """Persist before-values while the old installation is still intact."""
    with q.maintenance_owner(
        ctx.data, wait_s=0, status=q.EraseStatus()
    ) as owner:
        before = baseline.installation(ctx, rec)
        with q.maintenance_writer(owner):
            source = baseline.source_state(ctx)
        ctx.rdir.mkdir(mode=0o700)
        file_sync.sync_path(ctx.rdir.parent)
        rec.update(claude={"settings": []}, codex=[], cursor=[])
        rec["legacy"] = {
            "version": 1,
            "baseline": baseline.BASELINE,
            "platform": ctx.platform,
            "phase": "intent",
            "publication": "not_started",
            "before": before,
            "source": source,
            "record_directory": baseline.directory(ctx.rdir, private=True),
        }
        state.outcome(ctx, rec, "legacy_staging")


def _source(ctx: Ctx, rec: Record, owner: q.MaintenanceOwner) -> None:
    """Verify the original complete source under the physical writer."""
    with q.maintenance_writer(owner):
        if baseline.source_state(ctx) != rec["legacy"]["source"]:
            raise StepFailedError("legacy_source_changed")


def _stage(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Order durable intent, exclusion, native stop and final boot arming."""
    prepare_release(ctx, rec)
    rec["steps"].append("prepare_release")
    rec["legacy"]["candidate"] = baseline.candidate(ctx, rec)
    state.phase(ctx, rec, "prepared")
    with q.maintenance_owner(
        ctx.data, wait_s=0, status=q.EraseStatus()
    ) as owner:
        _source(ctx, rec, owner)
        exclusion.fence_runtime(ctx, rec, check)
    # Darwin's native stop verifies release by acquiring both roots itself.
    exclusion.stop_service(ctx, rec, check)
    with q.maintenance_owner(
        ctx.data, wait_s=0, status=q.EraseStatus()
    ) as owner:
        check()
        _source(ctx, rec, owner)
        _retained(ctx, rec)
        exclusion.verify(ctx, rec)
        exclusion.service_absent(ctx)
        observed = boot(ctx)
        rec["legacy"]["boot"] = observed
        state.phase(ctx, rec, "armed")
        state.outcome(ctx, rec, "restart_required")


def _failed(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Keep partial exclusion and original inputs for explicit recovery."""
    failed_phase = rec["legacy"]["phase"]
    try:
        exclusion.contain(ctx, rec, check)
    except Exception:
        rec["legacy"]["containment"] = "unconfirmed"
    rec["legacy"]["failed_phase"] = failed_phase
    rec["recovery_required"] = True
    if "boot" in rec["legacy"]:
        rec["legacy"]["invalidated_boot"] = rec["legacy"].pop("boot")
    rec["legacy"]["phase"] = "recovery_required"
    state.outcome(ctx, rec, "recovery_required")


def stage_legacy_exclusion(ctx: Ctx, rec: Record) -> None:
    """Arm restart-required only after durable managed launch exclusion.

    The stable installer lock precedes pending discovery and every owner
    acquisition. No database publication or runtime witness is performed.

    Raises:
        StepFailedError: If the exact baseline or exclusion cannot be proven.
    """
    if ctx.platform not in ("darwin", "linux"):
        raise StepFailedError("legacy_boot_unsupported")
    if ctx.dry_run:
        ctx.say(
            "DRY-RUN: legacy upgrade requires managed exclusion and restart"
        )
        return
    with state.operation(ctx) as check:
        stage_owned(ctx, rec, check)


def stage_owned(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Stage exclusion beneath the caller's already acquired operation lock."""
    if state.pending(ctx) is not None:
        raise StepFailedError("legacy_operation_pending")
    if not selected(ctx):
        raise StepFailedError("legacy_selection_mismatch")
    refreshed = preflight(ctx, Path(rec["repo"]), rec["sha"])
    rec.clear()
    rec.update(refreshed)
    boot(ctx)
    _claim(ctx, rec)
    try:
        _stage(ctx, rec, check)
    except Exception:
        # Retain the triggering error if recovery cannot be persisted.
        try:
            _failed(ctx, rec, check)
        except Exception:
            ctx.say("recovery_required: durable outcome could not be updated")
        raise


def verify_legacy_resume(ctx: Ctx, rec: Record) -> None:
    """Verify a later boot without activating the store."""
    with state.operation(ctx) as check:
        verify_owned(ctx, rec, check)


def verify_owned(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Validate a pending boot while retaining the caller's operation lock."""
    pending = state.pending(ctx)
    if pending is None or pending[1] != rec:
        raise StepFailedError("legacy_record_changed")
    state.validate(ctx, rec)
    if rec["legacy"]["phase"] not in ("armed", "verified"):
        raise StepFailedError("legacy_recovery_required")
    if ctx.dry_run:
        ctx.say("DRY-RUN: would validate the pending exclusion and later boot")
        return
    try:
        _resume(ctx, rec, check)
    except Exception as exc:
        if str(exc) != "restart_required" and not ctx.dry_run:
            try:
                _failed(ctx, rec, check)
            except Exception:
                ctx.say("recovery_required: outcome update failed")
        raise


def _resume(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Keep strict source and fence checks across the entire boot boundary."""
    validate_resume_repo(ctx, Path(rec["repo"]), rec["sha"])
    with q.maintenance_owner(
        ctx.data, wait_s=0, status=q.EraseStatus()
    ) as owner:
        check()
        _source(ctx, rec, owner)
        _retained(ctx, rec)
        exclusion.verify(ctx, rec)
        exclusion.service_absent(ctx)
        observed = boot(ctx)
        original = rec["legacy"].get("boot")
        validate_boot(ctx, original)
        if observed == original:
            raise StepFailedError("restart_required")
        rec["legacy"]["resumed_boot"] = observed
        state.phase(ctx, rec, "verified")
        state.outcome(ctx, rec, "legacy_cutover_pending")


def recover(ctx: Ctx, rec: Record, check: state.OperationCheck) -> list[str]:
    """Preserve uncertain legacy state and never reactivate an old runtime."""
    state.validate(ctx, rec)
    if not ctx.dry_run:
        _failed(ctx, rec, check)
    return ["legacy_recovery_required: retained source; no activation"]


def resume_install(
    ctx: Ctx, repo: Path, sha: str, check: state.OperationCheck
) -> Record | None:
    """Discover and resume one existing operation under the held lock."""
    pending = state.pending(ctx) if ctx.upgrade else None
    if pending is None:
        pending = review_policy.pending(ctx)
    if pending is None:
        return None
    active, rec = pending
    if (
        "legacy" not in rec
        and rec["sha"] != sha
        and rec.get("policy_status") == "active"
        and rec.get("policy_activation", {}).get("phase") == "complete"
        and rec.get("failed", {}).get("step") == "verify"
    ):
        review_policy.verify_ready(active, rec)
        return None
    if rec["repo"] != str(repo) or rec["sha"] != sha:
        raise StepFailedError("legacy_candidate_changed")
    review_policy.frozen(active, rec, check_supplied=True)
    if "legacy" not in rec:
        validate_resume_repo(active, repo, sha)
        return rec if ctx.dry_run else resume_policy(active, rec, check)
    if rec["legacy"]["publication"] == "not_started":
        verify_owned(active, rec, check)
    else:
        validate_resume_repo(active, repo, sha)
        legacy_recovery.verify_resume(active, rec)
    return rec if ctx.dry_run else install_legacy(active, rec, check)
