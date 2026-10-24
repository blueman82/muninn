"""Report the full gate result from the Codex Stop hook."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_TIMEOUT = 540


def gate_python() -> Path:
    """Select this checkout's pinned validation interpreter.

    Returns:
        The project virtual environment's Python executable.

    Raises:
        FileNotFoundError: If the validation environment is missing.
    """
    venv = ROOT / ".venv"
    if sys.platform == "win32":
        candidate = venv / "Scripts" / "python.exe"
    else:
        candidate = venv / "bin" / "python3.13"
    config = venv / "pyvenv.cfg"
    if (
        venv.is_symlink()
        or candidate.parent.is_symlink()
        or config.is_symlink()
        or not config.is_file()
        or not candidate.is_file()
    ):
        raise FileNotFoundError("pinned_validation_runtime_missing")
    return candidate


def main() -> int:
    """Run the gate on every Stop, including repeated turns.

    Returns:
        Zero with compact JSON when the gate passes; 2 with a failure reason
        on stderr when it fails or cannot complete.
    """
    # Hook input never selects a checkout or bypasses a previous failure.
    try:
        done = subprocess.run(
            [str(gate_python()), "-m", "tools.check", "--full"],
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
