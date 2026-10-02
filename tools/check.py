"""Single entry point for every standards gate.

Run ``python3.13 -m tools.check`` for the stdlib rules (what the test suite
runs) and ``python3.13 -m tools.check --full`` for the whole gate: those
rules plus ruff, black, pyright and shellcheck. A missing tool is a failure,
never a skip, so a machine without the dev tools cannot pass the full gate.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from tools.standards import check_repo

ROOT = Path(__file__).resolve().parent.parent
PY_PATHS = ("pctx", "install", "tools", "tests")
TOOL_STEPS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check", *PY_PATHS),
    ("black", "--check", "--quiet", *PY_PATHS),
    ("pyright", *PY_PATHS),
    ("shellcheck", "bin/pctx"),
)
INSTALL_HINT = (
    "install the dev tools: python3.13 -m venv .venv && "
    ".venv/bin/pip install -r requirements-dev.txt"
)


def find_tool(name: str) -> str | None:
    """Locate a dev tool in ``.venv/bin`` first, then on PATH.

    Args:
        name: Executable name, such as ``ruff``.

    Returns:
        The absolute path, or None when the tool is not installed.
    """
    local = ROOT / ".venv" / "bin" / name
    return str(local) if local.exists() else shutil.which(name)


def run_tools() -> int:
    """Run the external gates and report each one.

    Returns:
        The number of failed or missing tools.
    """
    failures = 0
    for name, *args in TOOL_STEPS:
        exe = find_tool(name)
        if exe is None:
            print(f"MISSING {name}: {INSTALL_HINT}")
            failures += 1
            continue
        done = subprocess.run([exe, *args], cwd=ROOT, check=False)
        print(f"{'ok' if done.returncode == 0 else 'FAIL'} {name}")
        failures += done.returncode != 0
    return failures


def main(argv: list[str] | None = None) -> int:
    """Run the gates and print every violation.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        Zero when everything passes, otherwise 1.
    """
    parser = argparse.ArgumentParser(prog="tools.check")
    parser.add_argument("--full", action="store_true", help="also run tools")
    parser.add_argument(
        "--summary", action="store_true", help="count per file"
    )
    args = parser.parse_args(argv)
    found = check_repo(ROOT)
    if args.summary:
        for path, count in sorted(Counter(v.path for v in found).items()):
            print(f"{count:5} {path}")
    else:
        for violation in found:
            print(violation)
    print(f"standards: {len(found)} violation(s)")
    failed = run_tools() if args.full else 0
    return 1 if found or failed else 0


if __name__ == "__main__":
    sys.exit(main())
