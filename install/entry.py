"""Native installer entrypoint using the already validated launcher Python."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from install import configedit as ce
from install import installer
from install.constants import PRIVATE_UMASK, TS_FORMAT
from install.context import Ctx, StepFailedError
from install.failure_text import explain


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    ap = argparse.ArgumentParser(prog="install.installer")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--dry-run", "--check", action="store_true")
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


def install_main(argv: Sequence[str] | None = None) -> int:
    """Run the installer from the command line.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        0 on success, 1 when the install failed or was refused.

    Raises:
        sqlite3.Error: If an ordinary install raises a database error.
    """
    if argv is None:
        argv = sys.argv[1:]
    ap = parser()
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
    )
    ctx.log_path = None if args.dry_run else ctx.lib / "install.log"
    return _run(ctx, args)


def _run(ctx: Ctx, args: argparse.Namespace) -> int:
    """Run one parsed install and print a failure with its explanation.

    Args:
        ctx: The context of the run.
        args: The parsed installer arguments.

    Returns:
        0 on success, 1 when the install failed or was refused.

    Raises:
        sqlite3.Error: If an ordinary install raises a database error.
    """
    try:
        ctx.cursor_hooks = installer.choose_cursor_hooks(ctx)
        installer.install(ctx, args.repo.resolve(), args.sha)
    except (StepFailedError, ce.RefusedError, ce.RacedError, OSError) as exc:
        return fail(str(exc))
    return 0


def fail(detail: str) -> int:
    """Print one failure code and its fixed plain explanation.

    Args:
        detail: The printed failure detail.

    Returns:
        The failure exit code 1.
    """
    print(f"FAILED: {detail}")
    for line in explain(detail):
        print(line)
    return 1
