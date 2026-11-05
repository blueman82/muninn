"""Native installer entrypoint using the already validated launcher Python."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from install import (
    codex_interface,
    codex_interface_recovery,
    host_renewal,
    host_renewal_recovery,
    installer,
    learning_scope,
    uninstall,
)
from install import configedit as ce
from install.constants import GIT_ENV, PRIVATE_UMASK, TS_FORMAT
from install.context import Ctx, StepFailedError, run_real
from install.host_renewal_preview import preview
from muninn.platform_paths import read_selection

ROOT = Path(__file__).resolve().parent.parent


def _head() -> str:
    """Read this checkout's exact commit without inherited git redirection."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        env=GIT_ENV,
        check=True,
        timeout=30,
    )
    return result.stdout.decode().strip()


def main(argv: Sequence[str] | None = None) -> int:
    """Choose fresh or upgrade while preserving the advanced installer API."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(
        arg in {"--renew-codex-host", "--codex-interface"}
        or arg.startswith(("--renew-codex-host=", "--codex-interface="))
        for arg in args
    ):
        return installer.main(["--repo", str(ROOT), "--sha", _head(), *args])
    if args and args[0] == "--uninstall":
        return uninstall.main(args[1:])
    if args and args[0] not in {"--check", "--status", "--help", "-h"}:
        return installer.main(args)
    if args in (["--help"], ["-h"]):
        print("usage: muninn-install [--check | --status | --uninstall]")
        print("Other arguments pass through to install.installer.")
        print(
            "Custom first policy: --repo DIR --sha SHA "
            "--fresh|--upgrade --review-policy PATH"
        )
    elif args not in ([], ["--check"], ["--status"]):
        return installer.main(args)
    else:
        return _automatic(args)
    return 0


def _automatic(args: list[str]) -> int:
    """Plan or run the inferred install mode against the exact source HEAD."""
    ctx = Ctx(Path.home(), run_real, "status", default_home=True)
    selection = read_selection(ctx.lib.parent)
    installed = selection[0].name if selection else None
    head = _head()
    if args == ["--status"]:
        print(f"installed {installed or 'none'}; checkout {head}")
        return 0
    if bool(installed) != os.path.lexists(ctx.data):
        print(
            "muninn-install: half-installed state; repair before installing",
            file=sys.stderr,
        )
        return 1
    mode = "--upgrade" if installed else "--fresh"
    flags = ["--repo", str(ROOT), "--sha", head, mode]
    if args == ["--check"]:
        flags.append("--dry-run")
    return installer.main(flags)


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    ap = argparse.ArgumentParser(prog="install.installer")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--dry-run", "--check", action="store_true")
    ap.add_argument("--renew-codex-host")
    ap.add_argument("--codex-interface")
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
    ap.add_argument("--learning-scope", choices=("now", "all"))
    ap.add_argument("--enable-learning", action="store_true")
    return ap


def install_main(argv: Sequence[str] | None = None) -> int:
    """Run the installer from the command line.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        0 on success, 1 when the install failed or was refused.

    Raises:
        sqlite3.Error: If an ordinary install raises a database error.
    """
    ap = parser()
    args = ap.parse_args(argv)
    if args.renew_codex_host == "preview" or args.codex_interface == "preview":
        args.dry_run = True
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
    ctx = installer.Ctx(
        args.home,
        installer.run_real,
        ts,
        dry_run=args.dry_run,
        probe=installer.codex_probe,
        say=print,
        fresh=args.fresh,
        upgrade=args.upgrade,
        default_home=default_home,
        review_policy=args.review_policy,
        enable_learning=args.enable_learning,
        renew_codex_host=args.renew_codex_host,
        codex_interface=args.codex_interface,
    )
    ctx.log_path = None if args.dry_run else ctx.lib / "install.log"
    try:
        if args.codex_interface is not None:
            _interface_input(ctx, args)
        elif args.renew_codex_host is not None:
            _renewal_input(ctx, args)
        else:
            ctx.cursor_hooks = installer.choose_cursor_hooks(ctx)
            learning_scope.choose(ctx, args.learning_scope, input)
        result = installer.install(ctx, args.repo.resolve(), args.sha)
        if result.get("legacy_outcome"):
            return 1
    except sqlite3.Error:
        if ctx.codex_interface is not None:
            print("FAILED: codex_interface_recovery_required")
            return 1
        if ctx.renew_codex_host is None:
            raise
        print("FAILED: host_renewal_recovery_required")
        return 1
    except (StepFailedError, ce.RefusedError, ce.RacedError, OSError) as exc:
        detail = (
            _interface_detail(exc)
            if ctx.codex_interface
            else (
                "host_renewal_recovery_required"
                if ctx.renew_codex_host
                else str(exc)
            )
        )
        print(f"FAILED: {detail}")
        return 1
    return 0


def _interface_detail(error: Exception) -> str:
    """Expose finite refusal codes without private I/O or proof text."""
    code = str(error)
    allowed = {
        "runtime_selection_unavailable",
        "runtime_selection_unsafe",
        "runtime_interface_unsupported",
        "runtime_probe_unavailable",
        "runtime_structure_limit",
        "codex_interface_delta",
        "codex_interface_plan_changed",
        "codex_interface_approval_required",
        "codex_interface_state_changed",
        "codex_interface_record_changed",
        "codex_interface_runtime_required",
        "codex_interface_recovery_required",
        "codex_interface_record_invalid",
        "codex_interface_limit",
        "codex_interface_decision_unsupported",
    }
    return code if code in allowed else "codex_interface_recovery_required"


def _interface_input(ctx: Ctx, args: argparse.Namespace) -> None:
    """Admit only the new upgrade route without unrelated policy changes."""
    if (
        args.fresh
        or not args.upgrade
        or args.enable_learning
        or args.learning_scope is not None
        or args.review_policy is not None
        or args.renew_codex_host is not None
    ):
        raise StepFailedError("codex_interface_delta")
    if ctx.dry_run:
        if args.codex_interface != "preview":
            raise StepFailedError("codex_interface_plan_changed")
        return
    found = codex_interface_recovery.pending(ctx)
    if found is not None:
        value = codex_interface.policy_records.codex_interface(
            found[1]["codex_interface"]
        )
        if args.codex_interface != value.plan.digest:
            raise StepFailedError("codex_interface_plan_changed")
        ctx.interface_plan = value.plan
        return
    plan = codex_interface.preview(ctx)
    if args.codex_interface != plan.digest:
        raise StepFailedError("codex_interface_plan_changed")
    ctx.interface_plan = plan


def _renewal_input(ctx: Ctx, args: argparse.Namespace) -> None:
    """Validate the explicit renewal route and collect fresh owner approval.

    Raises:
        StepFailedError: If the route, exact plan or approval is invalid.
    """
    ctx.renew_codex_host = args.renew_codex_host
    if (
        args.learning_scope is not None
        or args.enable_learning
        or args.fresh
        or not args.upgrade
        or args.review_policy is None
    ):
        raise StepFailedError("host_renewal_delta")
    if not ctx.dry_run and host_renewal_recovery.pending(ctx) is None:
        plan = preview(ctx, args.repo.resolve(), args.sha)
        if args.renew_codex_host != plan.digest:
            raise StepFailedError("host_renewal_plan_changed")
        if not sys.stdin.isatty():
            raise StepFailedError("host_renewal_approval_required")
        ctx.host_approval = host_renewal.confirm(plan, input, float(ctx.now()))
        ctx.host_plan = plan


if __name__ == "__main__":
    raise SystemExit(main())
