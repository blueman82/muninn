"""Observe one owned Darwin child's native exit event and separate reap."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import select
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

OVERALL_SECONDS = 12.0
CHILD_SECONDS = 8.0
FRAME_LIMIT = 512
_OS: Any = os
_KQ: Any = select
_SIG: Any = signal
_RETAINED_CHILDREN: list[subprocess.Popen[bytes]] = []
_EVIDENCE_ERRORS: list[int] = []


def _remaining(deadline: float) -> float:
    """Require positive time inside a fixed monotonic deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("stage_deadline")
    return remaining


def _frame(fd: int, deadline: float) -> bytes:
    """Read exactly one bounded pipe frame without blocking past its budget."""
    data = bytearray()
    with selectors.DefaultSelector() as ready:
        ready.register(fd, selectors.EVENT_READ)
        while True:
            if not ready.select(_remaining(deadline)):
                raise TimeoutError("frame_deadline")
            _remaining(deadline)
            try:
                chunk = os.read(fd, FRAME_LIMIT + 1)
            except BlockingIOError:
                continue
            _remaining(deadline)
            if not chunk:
                raise EOFError("command_eof")
            data.extend(chunk)
            if len(data) > FRAME_LIMIT:
                raise ValueError("frame_limit")
            if b"\n" in data:
                if not data.endswith(b"\n") or data.count(b"\n") != 1:
                    raise ValueError("frame_shape")
                return bytes(data[:-1])


def _send(fd: int, body: bytes, deadline: float) -> None:
    """Write a small bounded frame through a nonblocking pipe."""
    data = body + b"\n"
    if len(data) > FRAME_LIMIT:
        raise ValueError("frame_limit")
    with selectors.DefaultSelector() as ready:
        ready.register(fd, selectors.EVENT_WRITE)
        while data:
            if not ready.select(_remaining(deadline)):
                raise TimeoutError("write_deadline")
            _remaining(deadline)
            try:
                data = data[os.write(fd, data) :]
            except BlockingIOError:
                continue
            _remaining(deadline)


def _child(command: int, acknowledgement: int, expiry: float) -> int:
    """Wait for private commands and exit on EOF or lifetime expiry."""
    deadline = min(expiry, time.monotonic() + CHILD_SECONDS)
    try:
        hello = _frame(command, deadline).split()
        if len(hello) != 2 or hello[0] != b"HELLO" or len(hello[1]) != 32:
            return 2
        nonce = hello[1]
        identity = f"{os.getpid()} {_OS.getpgrp()} {_OS.getsid(0)}".encode()
        _send(acknowledgement, b"READY " + nonce + b" " + identity, deadline)
        while True:
            request = _frame(command, deadline)
            if request == b"LIVE " + nonce:
                _send(acknowledgement, b"LIVE " + nonce, deadline)
            elif request == b"EXIT " + nonce:
                return 0
            else:
                return 2
    except (OSError, EOFError, TimeoutError, ValueError):
        return 3
    finally:
        os.close(command)
        os.close(acknowledgement)


def _pipe() -> tuple[int, int]:
    """Create private nonblocking pipe descriptors."""
    reading, writing = os.pipe()
    os.set_blocking(reading, False)
    os.set_blocking(writing, False)
    return reading, writing


def _log(log: TextIO, code: str, **fields: int | float | bool) -> None:
    """Flush one code-and-count event without private protocol contents."""
    try:
        log.write(json.dumps({"code": code, **fields}, sort_keys=True) + "\n")
        log.flush()
    except OSError as error:
        _EVIDENCE_ERRORS.append(error.errno or 0)


def _events(
    queue: Any,
    pid: int,
    cookie: int,
    deadline: float,
    log: TextIO,
    result: dict[str, object],
) -> bool:
    """Accept only a genuine matching native process exit event."""
    events = queue.control(None, 2, _remaining(deadline))
    valid = len(events) == 1
    for event in events:
        fields = {
            name: getattr(event, name)
            for name in ("ident", "filter", "flags", "fflags", "data", "udata")
        }
        _log(log, "native_event", **fields)
        valid = valid and (
            event.ident == pid
            and event.filter == _KQ.KQ_FILTER_PROC
            and event.udata == cookie
            and bool(event.fflags & _KQ.KQ_NOTE_EXIT)
            and not bool(event.flags & _KQ.KQ_EV_ERROR)
        )
    _log(log, "event_batch", count=len(events), accepted=valid)
    result["exit_event_observed"] = valid
    _log(log, "event_deadline", on_time=time.monotonic() < deadline)
    _remaining(deadline)
    return valid


def _terminal_until(pid: int, deadline: float) -> tuple[bool, bool]:
    """Observe terminal child status without releasing its identity."""
    while time.monotonic() < deadline:
        exited = _OS.waitid(
            _OS.P_PID, pid, _OS.WEXITED | _OS.WNOHANG | _OS.WNOWAIT
        )
        on_time = time.monotonic() < deadline
        if (
            exited is not None
            and exited.si_pid == pid
            and exited.si_code
            in {_OS.CLD_EXITED, _OS.CLD_KILLED, _OS.CLD_DUMPED}
        ):
            return True, on_time
        if not on_time:
            return False, False
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.01, remaining))
    return False, time.monotonic() <= deadline


def _fallback(
    child: subprocess.Popen[bytes],
    identity_established: bool,
    overall: float,
    queue: Any,
    cookie: int,
    result: dict[str, object],
    log: TextIO,
) -> None:
    """Observe command-EOF cleanup before any previously authorized signal."""
    deadline = min(overall - 2.0, time.monotonic() + 3.0)
    cooperative = min(deadline, time.monotonic() + 0.5)
    if not identity_established:
        cooperative = overall - 2.0
    terminal = False
    try:
        terminal, on_time = _terminal_until(child.pid, cooperative)
        _log(log, "cooperative_phase", terminal=terminal, on_time=on_time)
    except OSError as error:
        _log(log, "cooperative_observation_error", errno=error.errno or 0)
    if not terminal and identity_established:
        try:
            if (
                child.returncode is not None
                or _OS.getpgid(child.pid) != child.pid
                or _OS.getsid(child.pid) != child.pid
            ):
                raise ValueError("fallback_group_unverified")
            _remaining(deadline)
            _OS.killpg(child.pid, _SIG.SIGKILL)
            result["fallback_signal_sent"] = True
            _log(log, "fallback_owned_group_signal", pgid=child.pid)
            _remaining(deadline)
        except (OSError, TimeoutError, ValueError) as error:
            _log(log, "signal_refused", errno=getattr(error, "errno", 0) or 0)
    elif not identity_established:
        _log(log, "preidentity_signal_forbidden")
    if queue is not None and not result["exit_event_observed"]:
        try:
            result["exit_event_observed"] = _events(
                queue, child.pid, cookie, deadline, log, result
            )
        except (OSError, TimeoutError):
            _log(log, "fallback_event_unresolved")
    try:
        reap = min(overall - 1.0, time.monotonic() + 1.0)
        status = child.wait(timeout=_remaining(reap))
        result["direct_child_reaped"] = True
        result["normal_exit_status"] = status
        on_time = time.monotonic() < reap
        _log(log, "fallback_reap", status=status, on_time=on_time)
        _remaining(reap)
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        result["ownership_abandoned_on_parent_exit"] = child.returncode is None
        _log(log, "fallback_reap_unresolved")


def _ready(
    child: subprocess.Popen[bytes],
    command: int,
    acknowledgement: int,
    nonce: bytes,
    deadline: float,
    log: TextIO,
) -> None:
    """Verify private readiness and native identity before signals."""
    _send(command, b"HELLO " + nonce, deadline)
    expected = (
        b"READY " + nonce + f" {child.pid} {child.pid} {child.pid}".encode()
    )
    if _frame(acknowledgement, deadline) != expected:
        raise ValueError("ready_identity")
    if (
        _OS.getpgid(child.pid) != child.pid
        or _OS.getsid(child.pid) != child.pid
    ):
        raise ValueError("native_identity")
    _log(log, "ready_deadline", on_time=time.monotonic() < deadline)
    _remaining(deadline)
    _log(log, "ready_verified", pid=child.pid, pgid=child.pid, sid=child.pid)


def _watch(
    child: subprocess.Popen[bytes],
    queue: Any,
    protocol: tuple[int, int, bytes],
    cookie: int,
    deadlines: tuple[float, float],
    result: dict[str, object],
    log: TextIO,
) -> None:
    """Register while held alive, observe native exit, then separately reap."""
    command, acknowledgement, nonce = protocol
    overall, registration = deadlines
    change = _KQ.kevent(
        child.pid,
        filter=_KQ.KQ_FILTER_PROC,
        flags=_KQ.KQ_EV_ADD | _KQ.KQ_EV_ENABLE | _KQ.KQ_EV_ONESHOT,
        fflags=_KQ.KQ_NOTE_EXIT,
        udata=cookie,
    )
    queue.control([change], 0, 0)
    result["registered"] = True
    _log(log, "registration_deadline", on_time=time.monotonic() < registration)
    _remaining(registration)
    _send(command, b"LIVE " + nonce, registration)
    if _frame(acknowledgement, registration) != b"LIVE " + nonce:
        raise ValueError("live_ack")
    _remaining(registration)
    _log(log, "registered_while_live", pid=child.pid)
    normal = min(overall - 5.0, time.monotonic() + 3.0)
    _send(command, b"EXIT " + nonce, normal)
    result["exit_event_observed"] = _events(
        queue, child.pid, cookie, normal, log, result
    )
    if not result["exit_event_observed"]:
        raise ValueError("exit_event_unverified")
    reap = min(overall - 4.0, time.monotonic() + 1.0)
    status = child.wait(timeout=_remaining(reap))
    result["direct_child_reaped"] = True
    result["normal_exit_status"] = status
    on_time = time.monotonic() < reap
    _log(log, "normal_reap", status=status, on_time=on_time)
    _remaining(reap)
    if status == 0 and time.monotonic() < overall:
        result["qualification"] = "positive_direct_child_only"


def _experiment(log: TextIO) -> dict[str, object]:
    """Run the single direct-child watch experiment with bounded cleanup."""
    began = time.monotonic()
    overall = began + OVERALL_SECONDS
    result: dict[str, object] = {
        "qualification": "inconclusive",
        "registered": False,
        "exit_event_observed": False,
        "direct_child_reaped": False,
        "cleanup_complete": False,
        "normal_exit_status": None,
        "fallback_signal_sent": False,
        "identity_established": False,
        "ownership_abandoned_on_parent_exit": False,
    }
    command_read, command_write = _pipe()
    acknowledgement_read, acknowledgement_write = _pipe()
    child: subprocess.Popen[bytes] | None = None
    queue: Any = None
    identity_established = False
    open_descriptors = [
        command_read,
        command_write,
        acknowledgement_read,
        acknowledgement_write,
    ]
    nonce = secrets.token_hex(16).encode()
    cookie = secrets.randbelow(2**31 - 1) + 1
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).resolve()),
                "--child",
                str(command_read),
                str(acknowledgement_write),
                str(began + CHILD_SECONDS),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            pass_fds=(command_read, acknowledgement_write),
            start_new_session=True,
        )
        _RETAINED_CHILDREN.append(child)
        for unused in (command_read, acknowledgement_write):
            os.close(unused)
            open_descriptors.remove(unused)
        result["pid"] = child.pid
        setup = min(overall - 8.0, began + 2.0)
        _ready(child, command_write, acknowledgement_read, nonce, setup, log)
        identity_established = True
        result["identity_established"] = True
        registration = min(overall - 7.0, time.monotonic() + 1.0)
        queue = _KQ.kqueue()
        _remaining(registration)
        _watch(
            child,
            queue,
            (command_write, acknowledgement_read, nonce),
            cookie,
            (overall, registration),
            result,
            log,
        )
    except (OSError, EOFError, ValueError, subprocess.TimeoutExpired) as error:
        _log(log, "inconclusive", errno=getattr(error, "errno", 0) or 0)
    except KeyboardInterrupt:
        _log(log, "experiment_interrupted")
    finally:
        os.close(command_write)
        open_descriptors.remove(command_write)
        if child is not None and child.returncode is None:
            _fallback(
                child,
                identity_established,
                overall,
                queue if result["registered"] else None,
                cookie,
                result,
                log,
            )
        if queue is not None:
            queue.close()
        for descriptor in open_descriptors:
            os.close(descriptor)
        result["cleanup_complete"] = bool(result["direct_child_reaped"])
        result["elapsed_seconds"] = time.monotonic() - began
        result["evidence_error_count"] = len(_EVIDENCE_ERRORS)
        if _EVIDENCE_ERRORS or time.monotonic() >= overall:
            result["qualification"] = "inconclusive"
    return result


def main() -> int:
    """Dispatch child mode or write exclusive-create evidence artifacts."""
    if sys.platform != "darwin":
        print(json.dumps({"qualification": "unsupported", "child_count": 0}))
        return 2
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        expiry = float(sys.argv[4])
        if not math.isfinite(expiry):
            return 2
        return _child(int(sys.argv[2]), int(sys.argv[3]), expiry)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    with args.result.open("x", encoding="utf-8") as output:
        with args.log.open("x", encoding="utf-8") as log:
            result = _experiment(log)
        json.dump(result, output, sort_keys=True)
        output.write("\n")
        output.flush()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["qualification"] == "positive_direct_child_only" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        # Avoid implicit polling even when evidence output itself fails.
        if any(child.returncode is None for child in _RETAINED_CHILDREN):
            os._exit(2)
