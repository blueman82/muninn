"""Report the full gate result from the Codex Stop hook."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_TIMEOUT = 540


def main_checkout() -> Path:
    """Find the primary checkout that owns this worktree's Git directory.

    Returns:
        The primary checkout, or this checkout if Git cannot locate it.
    """
    try:
        done = subprocess.run(
            [
                "git",
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ROOT
    if done.returncode:
        return ROOT
    return Path(done.stdout.strip()).parent


def gate_python() -> str:
    """Prefer this checkout's development interpreter for the full gate.

    Returns:
        The checkout's virtual-environment interpreter, or the current one.
    """
    relative = (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python3.13"
    )
    python = ROOT / ".venv" / relative
    if python.is_file():
        return str(python)
    python = main_checkout() / ".venv" / relative
    if python.is_file():
        return str(python)
    return sys.executable


def main() -> int:
    """Run the gate on every Stop, including repeated turns.

    Returns:
        Zero with compact JSON when the gate passes; 2 with a failure reason
        on stderr when it fails or cannot complete.
    """
    # Hook input never selects a checkout or bypasses a previous failure.
    try:
        done = subprocess.run(
            [gate_python(), "-m", "tools.check", "--full"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GATE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"Full gate could not complete: {exc}. "
            "Fix the cause or ask the owner for help before retrying.",
            file=sys.stderr,
        )
        return 2
    if done.returncode != 0:
        print(
            "Full gate failed; fix the failures or ask the owner for help "
            "before retrying. Completion requires a passing gate.\n"
            f"{done.stdout}{done.stderr}",
            file=sys.stderr,
        )
        return 2
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
