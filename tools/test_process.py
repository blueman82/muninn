"""Supervise test modules with a finite deadline and native child cleanup."""

from __future__ import annotations

import contextlib
import locale
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

from tools.process_quiescence import group_quiescent, sole_zombie

TIMEOUT_EXIT = 124
CANCELLED_EXIT = 130
CLEANUP_SECONDS = 5


def _interrupted(deadline: float, cancel: threading.Event) -> int | None:
    """Return a failing status when cancellation or the deadline is due."""
    if cancel.is_set():
        return CANCELLED_EXIT
    return TIMEOUT_EXIT if time.monotonic() >= deadline else None


def _contain(process: subprocess.Popen[bytes]) -> None:
    """Kill the owned Unix group and retain its leader through observation.

    Darwin observes the current group snapshot. Closed-session quiescence and
    exclusion of later same-session group joins remain unverified. Signals
    stay confined to the owned process group.

    Args:
        process: Owned group leader whose PID is reserved until observation.

    Raises:
        OSError: If signaling the owned group fails.
        PermissionError: If group signaling is denied without zombie proof.
        subprocess.TimeoutExpired: If cleanup exhausts its execution budget.
    """
    deadline = time.monotonic() + CLEANUP_SECONDS
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if not sole_zombie(process.pid):
            raise
    while time.monotonic() < deadline:
        verified = group_quiescent(process.pid)
        remaining = deadline - time.monotonic()
        # Late evidence must not release the reserved leader after expiry.
        if verified and remaining > 0:
            process.wait(timeout=remaining)
            if time.monotonic() >= deadline:
                break
            return
        if remaining <= 0:
            break
        time.sleep(min(0.01, remaining))
    raise subprocess.TimeoutExpired(process.args, CLEANUP_SECONDS)


def posix_module(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    deadline: float,
    cancel: threading.Event,
    streams: tuple[BinaryIO, BinaryIO],
) -> tuple[int, bool]:
    """Observe completion without reaping or waiting for descendant pipes."""
    with contextlib.ExitStack() as cleanup:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=streams[0],
            stderr=streams[1],
            start_new_session=True,
        )
        cleanup.callback(_contain, process)
        while True:
            interrupted = _interrupted(deadline, cancel)
            if interrupted is not None:
                return interrupted, True
            # WNOWAIT reserves the child's PID/PGID until group cleanup.
            exited = os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            if exited is not None:
                break
            cancel.wait(0.01)
    assert process.returncode is not None
    return process.returncode, False


def execute(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    timeout: float,
    cancel: threading.Event,
) -> tuple[int, str, bool]:
    """Run one module with bounded supervision and separate captured streams.

    Args:
        argv: Interpreter and unittest arguments.
        root: Working directory for test discovery.
        env: Environment with repository-specific Git variables removed.
        timeout: Finite positive module execution limit in seconds.
        cancel: Shared cancellation signal, checked before launching.

    Returns:
        Exit status, stdout followed by stderr, and whether supervision stopped
        the module before normal completion.
    """
    if cancel.is_set():
        return CANCELLED_EXIT, "", True
    deadline = time.monotonic() + timeout
    with (
        tempfile.TemporaryDirectory() as temporary,
        (Path(temporary) / "stdout").open("w+b") as stdout,
        (Path(temporary) / "stderr").open("w+b") as stderr,
    ):
        supervise = posix_module
        failure = ""
        try:
            code, interrupted = supervise(
                argv, root, env, deadline, cancel, (stdout, stderr)
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            code, interrupted = 127, False
            failure = (
                f"\n{argv[-1]}: launch failed or cleanup failed: {error}\n"
            )
        stdout.seek(0)
        stderr.seek(0)
        encoding = "utf-8" if sys.flags.utf8_mode else locale.getencoding()
        output = (
            (stdout.read() + stderr.read())
            .decode(encoding, errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    return code, output + failure, interrupted
