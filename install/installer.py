r"""Install muninn on this machine: --fresh (new) or --upgrade (re-pin).

Run from the repo root, at a clean commit:

  python3.13 -E -s -B -m install.installer \
      --repo DIR --sha SHA (--fresh | --upgrade) [--dry-run]

Any failure after the record is written runs ``install.rollback``. Every
external effect goes through ``ctx.run`` (launchctl, ps, git, muninn, codex),
so tests rehearse the whole sequence in a temp HOME. Each run ends with
exactly one pinned release.

The work is split by responsibility: ``constants`` and ``context`` (shared
names, the run context and command runner), ``record`` and ``rollback`` (the
run record and undoing it), ``preflight``, ``transforms`` and ``trust``
(checks, config edits and the Codex trust hash), ``steps_release``,
``steps_config``, ``probe`` and ``verify`` (the steps and the Codex probe)
and this module (the runner). The names the steps and tests use are
re-exported here.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from install import configedit as ce
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
from install.rollback import rollback
from install.snapshot import discard, snapshot_store
from install.steps_config import claude, codex, record
from install.steps_release import (
    fresh,
    history_upgrade,
    ingest_fresh,
    muninn_env,
    pin,
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
    "wait",
    "write_trust",
]

Step = Callable[[Ctx, Record], None]

FRESH_STEPS: tuple[Step, ...] = (
    pin,
    ingest_fresh,
    start_new,
    claude,
    codex,
    verify,
    prune,
    quiesce,
)
# The copy comes before pin: nothing may change if it cannot be made.
UPGRADE_STEPS: tuple[Step, ...] = (
    history_upgrade,
    quiesce,
    snapshot_store,
    pin,
    restart,
    verify,
    prune,
    quiesce,
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
        "error": f"{type(exc).__name__}: {exc}",
    }
    save(ctx, rec)
    ctx.say(f"install failed at {step.__name__}: {exc}; rolling back")
    for problem in rollback(ctx, rec):
        ctx.say(f"PROBLEM: {problem}")
    install_record(ctx, rec, "rolled_back")


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
    rec = preflight(ctx, Path(repo), sha)
    record(ctx, rec)
    step: Step = record
    try:
        for step in steps(ctx):
            step(ctx, rec)
            if not ctx.dry_run:
                rec["steps"].append(step.__name__)
                # Saved after every step so an interrupted run can roll back
                # exactly what had completed.
                save(ctx, rec)
    except Exception as exc:
        if ctx.dry_run:
            raise
        _roll_back(ctx, rec, step, exc)
        raise
    # After the last rollback chance: a failure only warns, and is noted in
    # the install record for support.
    rec["sweep_failed"] = sweep(ctx)
    discard(ctx, rec)
    if ctx.upgrade and not ctx.dry_run:  # no rollback once the old is gone
        shutil.rmtree(ctx.rdir)
        rec["record_removed"] = True
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
    # A dry run writes nothing, not even the log.
    say, run = (
        (print, run_real)
        if args.dry_run
        else install_log(
            Ctx(args.home, run_real, ts, default_home=default_home).lib
            / "install.log"
        )
    )
    ctx = Ctx(
        args.home,
        run,
        ts,
        dry_run=args.dry_run,
        probe=codex_probe,
        say=say,
        fresh=args.fresh,
        upgrade=args.upgrade,
        default_home=default_home,
    )
    try:
        install(ctx, args.repo.resolve(), args.sha)
    except (StepFailedError, ce.RefusedError, ce.RacedError) as exc:
        sink = say if ctx.rdir.exists() else print
        sink(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
