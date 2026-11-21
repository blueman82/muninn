"""Run one bounded provider process and report a content-free outcome."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CallOutcome:
    """Carry stdout on success, or one error code and no output."""

    stdout: bytes
    error_code: str | None


def run_call(
    argv: Sequence[str],
    prompt: bytes,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    limit: int,
) -> CallOutcome:
    """Feed the prompt to the process and collect bounded stdout.

    Args:
        argv: Full command line.
        prompt: Bytes written to standard input.
        cwd: Working directory of the process.
        env: Complete process environment.
        timeout: Seconds before the process group is killed.
        limit: Largest accepted stdout size in bytes.

    Returns:
        The outcome with one of the codes cli_missing, timeout,
        cli_failed or output_limit on failure.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
    except OSError:
        return CallOutcome(b"", "cli_missing")
    try:
        out, _ = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return CallOutcome(b"", "timeout")
    if proc.returncode != 0:
        return CallOutcome(b"", "cli_failed")
    if len(out) > limit:
        return CallOutcome(b"", "output_limit")
    return CallOutcome(out, None)
