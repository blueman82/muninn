"""Native installer entrypoint using the already validated launcher Python."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from install import installer, uninstall
from install.constants import GIT_ENV
from install.context import Ctx, run_real
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
    if args and args[0] == "--uninstall":
        return uninstall.main(args[1:])
    if args and args[0] not in {"--check", "--status", "--help", "-h"}:
        return installer.main(args)
    if args in (["--help"], ["-h"]):
        print("usage: muninn-install [--check | --status | --uninstall]")
        print("Other arguments pass through to install.installer.")
    elif args not in ([], ["--check"], ["--status"]):
        return installer.main(args)
    else:
        return _automatic(args)
    return 0


def _automatic(args: list[str]) -> int:
    """Plan or run the inferred install mode against the exact source HEAD."""
    if args not in ([], ["--check"], ["--status"]):
        return installer.main(args)
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


if __name__ == "__main__":
    raise SystemExit(main())
