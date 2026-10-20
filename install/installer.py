"""Install one prepared release using explicit fresh or upgrade mode.

External effects use the context runner. Every possible store exposure
preserves actual data and its private recovery authority on failure.
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path

from install import configedit as ce
from install import (
    legacy_state,
    legacy_transition,
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
    PRIVATE_UMASK,
    TRUST,
    TS_FORMAT,
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
from install.rollback import rollback_owned
from install.runtime import prepare_runtime, verify_runtime
from install.snapshot import discard, snapshot_store
from install.steps_config import (
    choose_cursor_hooks,
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
    pin,
    prepare_release,
    prune,
    quiesce,
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
    quiesce,
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


def _roll_back(ctx: Ctx, rec: Record, step: Step, exc: Exception) -> None:
    """Record a failed step and undo the install.

    Args:
        ctx: The run context.
        rec: The run record.
        step: The step that failed.
        exc: What it raised.
    """
    rec["failed"] = {
        "step": step.__name__,
        "error": type(exc).__name__,
    }
    save(ctx, rec)
    ctx.say(f"install failed at {step.__name__}: {exc}; rolling back")
    if step is not prepare_release:
        for problem in rollback_owned(ctx, rec):
            ctx.say(f"PROBLEM: {problem}")
    outcome = (
        "recovery_required" if rec.get("recovery_required") else "rolled_back"
    )
    install_record(ctx, rec, outcome)


def install(ctx: Ctx, repo: Path | str, sha: str) -> Record:
    """Run the whole install, rolling back if any step fails.

    Args:
        ctx: The run context.
        repo: The source repository, at a clean checkout of ``sha``.
        sha: The full commit id to install.

    Returns:
        The final run record.

    Raises:
        StepFailedError: If a precondition fails in preflight or a step
            fails; a failed step is re-raised after the rollback.
        RefusedError: If a provider config has a layout the edits refuse.
        RacedError: If a provider config changed while it was being edited.
        Exception: Any other step exception, re-raised after the rollback (in
            a dry run, re-raised without one).
    """
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


def _install(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck
) -> Record:
    """Run the ordinary route inside the caller's installer ownership."""
    log_path = ctx.log_path
    if log_path is not None and not ctx.dry_run:
        ctx = copy.copy(ctx)
        ctx.say, ctx.run = install_log(log_path, ctx.run)
    record(ctx, rec)
    step: Step = record
    try:
        with ExitStack() as maintenance:
            for step in steps(ctx):
                check()
                if step in (start_new, restart):
                    maintenance.close()
                    check()
                if step is prepare_store:
                    maintenance.enter_context(
                        prepared_install_store(ctx, rec, check)
                    )
                else:
                    step(ctx, rec)
                if not ctx.dry_run:
                    rec["steps"].append(step.__name__)
                    # Recovery needs the last completed step after a crash.
                    save(ctx, rec)
    except Exception as exc:
        if ctx.dry_run:
            raise
        try:
            check()
            _roll_back(ctx, rec, step, exc)
        except Exception:
            ctx.say("recovery_required: cleanup failed")
        raise
    check()
    return _finish(ctx, rec)


def _finish(ctx: Ctx, rec: Record) -> Record:
    """Retain activation authority and publish a value-free final outcome."""
    # After the last rollback chance: a failure only warns, and is noted in
    # the install record for support.
    rec["sweep_failed"] = sweep(ctx)
    discard(ctx, rec)
    if ctx.upgrade and not ctx.dry_run and not rec.get("policy_activation"):
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


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    ap = argparse.ArgumentParser(prog="install.installer")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--dry-run", action="store_true")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--upgrade",
        action="store_true",
        help="already on muninn: re-pin this commit, restart, keep one"
        " release",
    )
    mode.add_argument(
        "--fresh",
        action="store_true",
        help="a new machine: no data, launchd job or release yet",
    )
    ap.add_argument("--home", type=Path)
    ap.add_argument("--review-policy", type=Path)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installer from the command line.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        0 on success, 1 when the install failed or was refused.
    """
    ap = _parser()
    args = ap.parse_args(argv)
    default_home = args.home is None
    args.home = args.home or Path.home()
    if (
        sys.platform == "darwin"
        and args.home.resolve() != Path.home().resolve()
        and not args.dry_run
    ):
        ap.error(
            "--home is dry-run only: launchctl acts on the real gui domain"
        )
    # Files we create must not be readable by other users.
    os.umask(PRIVATE_UMASK)
    ts = time.strftime(TS_FORMAT, time.gmtime())
    ctx = Ctx(
        args.home,
        run_real,
        ts,
        dry_run=args.dry_run,
        probe=codex_probe,
        say=print,
        fresh=args.fresh,
        upgrade=args.upgrade,
        default_home=default_home,
        review_policy=args.review_policy,
    )
    ctx.log_path = None if args.dry_run else ctx.lib / "install.log"
    ctx.cursor_hooks = choose_cursor_hooks(ctx)
    try:
        result = install(ctx, args.repo.resolve(), args.sha)
        if result.get("legacy_outcome"):
            return 1
    except (StepFailedError, ce.RefusedError, ce.RacedError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
