"""Block Codex completion until this worktree passes the full gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_TIMEOUT = 540


def main() -> int:
    """Run the gate on every Stop, including stops after a blocked turn.

    Returns:
        Zero with compact JSON when the gate passes; 2 with a blocking reason
        on stderr when it fails or cannot complete.
    """
    # Hook input never selects a checkout or bypasses a previous failure.
    try:
        done = subprocess.run(
            [sys.executable, "-m", "tools.check", "--full"],
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
