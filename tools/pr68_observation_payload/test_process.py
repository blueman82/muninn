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

from muninn import _cleanup_observation as observation
from muninn.review_process_posix import sole_zombie
from muninn.review_process_quiescence import group_quiescent
from muninn.review_process_windows import launch

TIMEOUT_EXIT = 124
CANCELLED_EXIT = 130
CLEANUP_SECONDS = 5


def _interrupted(deadline: float, cancel: threading.Event) -> int | None:
    """Return a failing status when cancellation or the deadline is due."""
    if cancel.is_set():
        return CANCELLED_EXIT
    return TIMEOUT_EXIT if time.monotonic() >= deadline else None


def _original_contain(process: subprocess.Popen[bytes]) -> None:
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
    if os.name == "nt":
        raise OSError("unix_process_group_unavailable")
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
    if os.name == "nt":
        raise OSError("unix_process_group_unavailable")
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


def _drain_pipe(pipe: BinaryIO, stream: BinaryIO) -> bool:
    """Copy one available chunk and report whether a pipe reached EOF."""
    try:
        chunk = os.read(pipe.fileno(), 65536)
    except BlockingIOError:
        return False
    if chunk:
        stream.write(chunk)
        return False
    return True


def windows_module(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    deadline: float,
    cancel: threading.Event,
    streams: tuple[BinaryIO, BinaryIO],
) -> tuple[int, bool]:
    """Drain native pipes while retaining the exclusive Windows Job."""
    with contextlib.ExitStack() as cleanup:
        child = launch(argv, root, env)
        cleanup.callback(child.close)
        child.stdin.close()
        pipes = (child.stdout, child.stderr)
        for pipe in pipes:
            os.set_blocking(pipe.fileno(), False)
        ended = [False, False]
        outcome: tuple[int, bool] | None = None
        cleanup_deadline = deadline
        while True:
            if outcome is None:
                interrupted = _interrupted(deadline, cancel)
                code = child.poll() if interrupted is None else interrupted
                if code is not None:
                    outcome = code, interrupted is not None
                    child.close_job()
                    cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
            elif time.monotonic() >= cleanup_deadline:
                raise OSError("test_output_cleanup_timeout")
            for index, pipe in enumerate(pipes):
                if not ended[index]:
                    ended[index] = _drain_pipe(pipe, streams[index])
            if outcome is not None and all(ended):
                return outcome
            cancel.wait(0.01)


def _original_execute(
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
        supervise = windows_module if os.name == "nt" else posix_module
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


def _observed_contain(process: subprocess.Popen[bytes]) -> None:
    """Observe the unchanged owned-group containment operations."""
    if os.name == "nt":
        raise OSError("unix_process_group_unavailable")
    origin = observation.mark("cleanup_origin", time.monotonic())
    deadline = observation.mark("cleanup_deadline", origin + CLEANUP_SECONDS)
    observation.mark("timeout_path", "before_observer")
    observation.mark("stage", "signal")
    try:
        os.killpg(process.pid, signal.SIGKILL)
        observation.mark("signal", "returned")
    except ProcessLookupError:
        observation.mark("signal", "ESRCH")
    except PermissionError:
        observation.mark("signal", "EPERM")
        if not sole_zombie(process.pid):
            raise
    observation.mark("stage", "cleanup_guard")
    while observation.mark("guard_sample", time.monotonic()) < deadline:
        observation.attempt(process.pid)
        observation.mark("timeout_path", "observer_false")
        observation.mark("stage", "observer")
        verified = observation.mark(
            "observer_result", group_quiescent(process.pid)
        )
        sample = observation.mark("remaining_sample", time.monotonic())
        remaining = observation.mark("remaining", deadline - sample)
        if verified and remaining > 0:
            observation.mark("wait_entered", True)
            observation.mark("wait_allowance", remaining)
            observation.mark("returncode_before", process.returncode)
            observation.mark("stage", "wait")
            status = process.wait(timeout=remaining)
            observation.mark("wait_status", status)
            observation.mark("returncode_after", process.returncode)
            observation.mark("actual_reaped", True)
            observation.mark("stage", "wait_returned")
            if (
                observation.mark("post_wait_sample", time.monotonic())
                >= deadline
            ):
                observation.mark("timeout_path", "late_wait_reaped")
                break
            observation.mark("timeout_path", "not_timed_out")
            return
        if remaining <= 0:
            if verified:
                observation.mark("timeout_path", "late_observer_true")
            break
        time.sleep(min(0.01, remaining))
    raise subprocess.TimeoutExpired(process.args, CLEANUP_SECONDS)


def observed_posix(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    deadline: float,
    cancel: threading.Event,
    streams: tuple[BinaryIO, BinaryIO],
) -> tuple[int, bool]:
    """Retain original native launch and terminal facts."""
    if os.name == "nt":
        raise OSError("unix_process_group_unavailable")
    with contextlib.ExitStack() as cleanup:
        observation.mark("launch_entered", True)
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=streams[0],
            stderr=streams[1],
            start_new_session=True,
        )
        observation.mark("launch_returned", True)
        observation.mark("pid", process.pid)
        cleanup.callback(_contain, process)
        while True:
            if cancel.is_set():
                interrupted = CANCELLED_EXIT
            else:
                sample = time.monotonic()
                observation.first("first_parent_now", sample)
                interrupted = TIMEOUT_EXIT if sample >= deadline else None
            if interrupted is not None:
                return interrupted, True
            observation.increment_meta("waitid_count")
            exited = os.waitid(
                os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            if exited is not None:
                observation.mark("terminal_pid", exited.si_pid)
                observation.mark("terminal_code", exited.si_code)
                observation.mark("terminal_status", exited.si_status)
                break
            cancel.wait(0.01)
    assert process.returncode is not None
    return process.returncode, False


def _observed_execute(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    timeout: float,
    cancel: threading.Event,
) -> tuple[int, str, bool]:
    """Retain the original catches, uncapped output reads and result tuple."""
    if cancel.is_set():
        return CANCELLED_EXIT, "", True
    origin = observation.mark("module_origin", time.monotonic())
    deadline = observation.mark("module_deadline", origin + timeout)
    with (
        tempfile.TemporaryDirectory() as temporary,
        (Path(temporary) / "stdout").open("w+b") as stdout,
        (Path(temporary) / "stderr").open("w+b") as stderr,
    ):
        supervise = windows_module if os.name == "nt" else observed_posix
        failure = ""
        try:
            code, interrupted = supervise(
                argv, root, env, deadline, cancel, (stdout, stderr)
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            observation.outer_error(error)
            code, interrupted = 127, False
            failure = (
                f"\n{argv[-1]}: launch failed or cleanup failed: {error}\n"
            )
        stdout.seek(0)
        stderr.seek(0)
        encoding = "utf-8" if sys.flags.utf8_mode else locale.getencoding()
        raw_stdout, raw_stderr = stdout.read(), stderr.read()
        observation.capture(raw_stdout, raw_stderr)
        output = (
            (raw_stdout + raw_stderr)
            .decode(encoding, errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    return code, output + failure, interrupted


def execute(
    argv: tuple[str, ...],
    root: Path,
    env: dict[str, str],
    timeout: float,
    cancel: threading.Event,
) -> tuple[int, str, bool]:
    """Observe only the selected worker and preserve original cancellation."""
    if argv[-1] != observation.TARGET:
        return _original_execute(argv, root, env, timeout, cancel)
    token = observation.begin()
    try:
        result = _observed_execute(argv, root, env, timeout, cancel)
    except BaseException:
        observation.restore(token)
        raise
    observation.restore(token)
    if result[0] == CANCELLED_EXIT:
        return result
    token.trace.meta["runner_code"] = result[0]
    try:
        observation.export(token.trace)
    except Exception as error:
        observation.export_failure(error)
    return result


def _contain(process: subprocess.Popen[bytes]) -> None:
    """Dispatch only the selected owned group to diagnostic containment."""
    if observation.active(process.pid):
        _observed_contain(process)
    else:
        _original_contain(process)
