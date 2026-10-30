"""Install one prepared release using explicit fresh or upgrade mode.

External effects use the context runner. Every possible store exposure
preserves actual data and its private recovery authority on failure.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path

from install import (
    entry,
    host_renewal,
    host_renewal_recovery,
    legacy_baseline,
    legacy_state,
    legacy_transition,
    policy_records,
)
from install.constants import (
    CLAUDE_EVENTS,
    CODEX_KEYS,
    CODEX_VERIFIED,
    EVENTS,
    GIT_ENV,
    HEARTBEAT_S,
    INSTALL_RECORD,
    LABEL,
    MARKERS,
    MARKETPLACE,
    MKT_NAME,
    OWNER_STEP,
    PINNED,
    PLIST,
    PLUGIN,
    PLUGIN_ID,
    TRUST,
)
from install.context import (
    Ctx,
    Runner,
    StepFailedError,
    install_log,
    is_new,
    job,
    must,
    run_real,
    wait,
)
from install.cutover import (
    prepare_store,
    prepared_install_store,
)
from install.finish import finalize
from install.host_renewal_operation import held_operation
from install.host_renewal_preview import preview, summary
from install.preflight import preflight
from install.probe import codex_probe
from install.record import (
    Record,
    codex_record,
    install_record,
    json_entry,
    load_record,
    save,
)
from install.recovery_guidance import recover_pause
from install.rollback import failed_install
from install.runtime import prepare_runtime, verify_runtime
from install.snapshot import snapshot_store
from install.steps_config import (
    choose_cursor_hooks as choose_cursor_hooks,
)
from install.steps_config import (
    claude,
    codex,
    cursor,
    record,
)
from install.steps_release import (
    fresh,
    history_upgrade,
    ingest_fresh,
    muninn_env,
    pause,
    pin,
    prepare_release,
    prune,
    relink,
    restart,
    start_new,
    sweep,
)
from install.transforms import (
    claude_paths,
    codex_check,
    codex_scan,
    drop_trust,
    edit_settings,
    enable,
    ours,
    repoint,
    set_line,
    write_trust,
)
from install.trust import LABELS, codex_hooks
from install.verify import codex_checks, hook_commands, verify
from muninn import platform_io

__all__ = [
    "CLAUDE_EVENTS",
    "CODEX_KEYS",
    "CODEX_VERIFIED",
    "EVENTS",
    "FRESH_STEPS",
    "GIT_ENV",
    "HEARTBEAT_S",
    "INSTALL_RECORD",
    "LABEL",
    "LABELS",
    "MARKERS",
    "MARKETPLACE",
    "MKT_NAME",
    "OWNER_STEP",
    "PINNED",
    "PLIST",
    "PLUGIN",
    "PLUGIN_ID",
    "TRUST",
    "UPGRADE_STEPS",
    "Ctx",
    "Record",
    "Runner",
    "StepFailedError",
    "claude",
    "claude_paths",
    "codex",
    "codex_check",
    "codex_checks",
    "codex_hooks",
    "codex_probe",
    "codex_record",
    "codex_scan",
    "cursor",
    "drop_trust",
    "edit_settings",
    "enable",
    "fresh",
    "history_upgrade",
    "hook_commands",
    "ingest_fresh",
    "install",
    "install_log",
    "install_record",
    "is_new",
    "job",
    "json_entry",
    "load_record",
    "main",
    "muninn_env",
    "must",
    "ours",
    "pin",
    "preflight",
    "prepare_release",
    "prepare_runtime",
    "prepare_store",
    "prune",
    "record",
    "recover_pause",
    "relink",
    "repoint",
    "restart",
    "run_real",
    "save",
    "set_line",
    "snapshot_store",
    "start_new",
    "steps",
    "sweep",
    "verify",
    "verify_runtime",
    "wait",
    "write_trust",
]

Step = Callable[[Ctx, Record], object]

FRESH_STEPS: tuple[Step, ...] = (
    prepare_release,
    prepare_store,
    pin,
    ingest_fresh,
    claude,
    codex,
    cursor,
    start_new,
    verify,
    prune,
)
# Store preparation precedes activation of the new runtime and hooks.
UPGRADE_STEPS: tuple[Step, ...] = (
    prepare_release,
    history_upgrade,
    pause,
    recover_pause,
    prepare_store,
    pin,
    claude,
    codex,
    cursor,
    restart,
    verify,
    prune,
)


def steps(ctx: Ctx) -> tuple[Step, ...]:
    """Choose the step sequence for this run's mode."""
    return UPGRADE_STEPS if ctx.upgrade else FRESH_STEPS


def install(ctx: Ctx, repo: Path | str, sha: str) -> Record:
    """Run the prepared step sequence under one installer operation.

    Args:
        ctx: Explicit installation paths and mode.
        repo: Source repository with the exact target release.
        sha: Full target commit identifier.

    Returns:
        The final validated installer record.

    Raises:
        StepFailedError: If current ownership or recovery state refuses.
    """
    if ctx.renew_codex_host is not None:
        return _renewal(ctx, Path(repo), sha)
    if host_renewal_recovery.pending(ctx) is not None:
        raise StepFailedError("host_renewal_recovery_required")
    if (
        ctx.upgrade
        and ctx.platform == "win32"
        and legacy_transition.selected(ctx)
    ):
        raise StepFailedError("legacy_boot_unsupported")
    if ctx.upgrade or ctx.lib.exists():
        with legacy_state.operation(ctx) as check:
            resumed = legacy_transition.resume_install(
                ctx, Path(repo), sha, check
            )
            if resumed is not None:
                return resumed
            rec = preflight(ctx, Path(repo), sha)
            if ctx.upgrade and legacy_transition.selected(ctx):
                if ctx.dry_run:
                    ctx.say("DRY-RUN: legacy exclusion and restart required")
                else:
                    legacy_transition.stage_owned(ctx, rec, check)
                return rec
            return _install(ctx, rec, check)
    rec = preflight(ctx, Path(repo), sha)
    if ctx.dry_run:
        return _install(ctx, rec, lambda: (0, 0))
    platform_io.ensure_private_dir(ctx.lib)
    with legacy_state.operation(ctx) as check:
        return _install(ctx, preflight(ctx, Path(repo), sha), check)


def _renewal(ctx: Ctx, repo: Path, sha: str) -> Record:
    """Route a checked renewal through the existing single step engine."""
    if ctx.dry_run:
        plan = preview(ctx, Path(repo), sha)
        ctx.say(json.dumps(summary(plan)))
        return {"host_preview": summary(plan)}
    ctx.host_final_steps = tuple(step.__name__ for step in steps(ctx))
    found = host_renewal_recovery.pending(ctx)
    saved_file = None
    if found is not None:
        active, saved = found
        receipt = policy_records.host_renewal(saved["host_renewal"])
        saved_file = legacy_baseline.file_state(
            active.rdir / "rollback-record.json", private=True
        )
        expected = receipt.held_operation_lock
        plan = receipt.plan
    else:
        plan = ctx.host_plan
        if plan is None:
            raise StepFailedError("host_renewal_approval_required")
        expected = plan.operation_lock
    with held_operation(
        ctx, approved=plan.operation_lock, expected_before=expected
    ) as operation:
        ctx.host_operation = operation
        if found is not None:
            active, saved = found
            repeated = host_renewal_recovery.pending(ctx)
            if (
                repeated is None
                or repeated[1] != saved
                or legacy_baseline.file_state(
                    active.rdir / "rollback-record.json", private=True
                )
                != saved_file
            ):
                raise StepFailedError("host_renewal_record_changed")
            resumed = host_renewal_recovery.resume_install(
                ctx, Path(repo), sha, operation.check
            )
            if resumed is None:
                raise StepFailedError("host_renewal_record_changed")
            active = dataclasses.replace(ctx, ts=resumed["ts"])
            if resumed["host_renewal"]["phase"] == "upgrade_verified":
                return host_renewal_recovery.finish_verified(
                    active, resumed, operation.check
                )
            return _install(active, resumed, operation.check, resume=True)
        return _install(ctx, preflight(ctx, Path(repo), sha), operation.check)


def _install(
    ctx: Ctx,
    rec: Record,
    check: legacy_state.OperationCheck,
    *,
    resume: bool = False,
) -> Record:
    """Run the ordinary route inside the caller's installer ownership."""
    log_path = ctx.log_path
    if log_path is not None and not ctx.dry_run:
        ctx = copy.copy(ctx)
        ctx.say, ctx.run = install_log(log_path, ctx.run)
    if not resume:
        record(ctx, rec)
        if ctx.host_plan is not None:
            host_renewal.stage(ctx, rec, ctx.host_plan, check)
    step: Step = record
    try:
        with ExitStack() as maintenance:
            for step in _pending_steps(ctx, rec, resume):
                _run_step(ctx, rec, step, maintenance, check)
    except Exception as exc:
        if ctx.dry_run:
            raise
        try:
            check()
            failed_install(ctx, rec, step.__name__, exc)
        except Exception:
            ctx.say("recovery_required: cleanup failed")
        raise
    check()
    return finalize(ctx, rec)


def main(argv: Sequence[str] | None = None) -> int:
    """Delegate advanced CLI parsing to the shared native entry module."""
    return entry.install_main(argv)


def _pending_steps(ctx: Ctx, rec: Record, resume: bool) -> tuple[Step, ...]:
    """Select unfinished steps while always rechecking owner and restart."""
    if not resume:
        return steps(ctx)
    return tuple(
        step
        for step in steps(ctx)
        if step.__name__ not in rec["steps"]
        or step in (prepare_store, restart, verify)
    )


def _run_step(
    ctx: Ctx,
    rec: Record,
    step: Step,
    maintenance: ExitStack,
    check: legacy_state.OperationCheck,
) -> None:
    """Apply one existing step while retaining or releasing its owner scope."""
    check()
    if step in (start_new, restart):
        maintenance.close()
        if "host_renewal" in rec:
            host_renewal.phase(ctx, rec, "restart_pending")
        check()
    if step is prepare_store:
        maintenance.enter_context(prepared_install_store(ctx, rec, check))
    else:
        step(ctx, rec)
    if not ctx.dry_run:
        if step.__name__ not in rec["steps"]:
            rec["steps"].append(step.__name__)
        # Recovery needs the last completed step after a crash.
        save(ctx, rec)


if __name__ == "__main__":
    raise SystemExit(main())
