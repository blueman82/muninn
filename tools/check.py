"""Single entry point for every standards gate.

Run ``python3.13 -m tools.check`` for the stdlib rules (what the test suite
runs) and ``python3.13 -m tools.check --full`` for the whole gate: those
rules plus ruff, black, pyright and shellcheck, a check that the git hooks in
``.githooks`` are switched on, and the whole test suite (run in parallel by
``tools.run_tests``). A missing tool, an unswitched hook or a failing test is a
failure, never a skip, so a machine without them cannot pass the full gate.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from tools.run_tests import main as run_suite
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
HOOKS_HINT = "switch the git hooks on: git config core.hooksPath .githooks"


def git_output(*args: str) -> str:
    """Run a read-only git command in the repository.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Trimmed standard output, or an empty string if git fails or is
        missing.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def main_checkout() -> Path:
    """Find the main working tree, which may differ from ``ROOT``.

    A linked worktree has no ``.venv`` of its own; the dev tools live in the
    main checkout, whose ``.git`` directory is the shared common dir.

    Returns:
        The main checkout, or ``ROOT`` when git cannot say.
    """
    common = git_output(
        "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    return Path(common).parent if common else ROOT


def find_tool(name: str) -> str | None:
    """Locate a dev tool in this checkout's venv, the main one, then PATH.

    Args:
        name: Executable name, such as ``ruff``.

    Returns:
        The absolute path, or None when the tool is not installed.
    """
    for base in (ROOT, main_checkout()):
        local = base / ".venv" / "bin" / name
        if local.exists():
            return str(local)
    return shutil.which(name)


def hooks_installed() -> bool:
    """Say whether this clone runs the hooks tracked in ``.githooks``.

    Git cannot track its own ``core.hooksPath``, so the gate checks it. The
    setting may be relative (``.githooks``, resolved per worktree) or an
    absolute path; what matters is that it names the tracked hooks folder of
    this checkout or of the main one.

    Returns:
        True when ``core.hooksPath`` resolves to a ``.githooks`` folder of
        this checkout or the main checkout.
    """
    configured = git_output("config", "core.hooksPath")
    if not configured:
        return False
    path = Path(configured)
    resolved = (path if path.is_absolute() else ROOT / path).resolve()
    wanted = {
        (base / ".githooks").resolve() for base in (ROOT, main_checkout())
    }
    return resolved in wanted


def run_tools() -> int:
    """Run the external gates and the test suite, reporting each one.

    Returns:
        The number of failed or missing tools plus one if a test failed.
    """
    failures = 0
    if not hooks_installed():
        print(f"FAIL git hooks: {HOOKS_HINT}")
        failures += 1
    for name, *args in TOOL_STEPS:
        exe = find_tool(name)
        if exe is None:
            print(f"MISSING {name}: {INSTALL_HINT}")
            failures += 1
            continue
        done = subprocess.run([exe, *args], cwd=ROOT, check=False)
        print(f"{'ok' if done.returncode == 0 else 'FAIL'} {name}")
        failures += done.returncode != 0
    # Last: the tests are the slowest step, and a lint failure is the faster
    # thing to learn about first.
    suite_failed = run_suite([]) != 0
    print(f"{'FAIL' if suite_failed else 'ok'} tests")
    return failures + suite_failed


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
