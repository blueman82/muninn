"""Retain bounded scalar diagnostics without changing native decisions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

type Scalar = str | bool | int | float | None
TARGET = "tests.test_install_legacy_record_context"
MISSING = "not_evaluated"
LIMIT = 65536
MAX_COUNT = 2**31 - 1
_LOCAL = threading.local()
_EXPORT_ERRORS: list[int] = []
_REASONS = (
    "accepted",
    "unknown",
    "exception",
    "visible",
    "terminal_a",
    "terminal_b",
    "deadline_ok",
    "counter_equal",
    "leader_present",
    "maps_equal",
    "members_terminal",
)
_META: dict[str, Scalar] = {
    "stage": MISSING,
    "pid": 0,
    "module_origin": MISSING,
    "module_deadline": MISSING,
    "launch_entered": False,
    "launch_returned": False,
    "first_parent_now": MISSING,
    "waitid_count": 0,
    "terminal_pid": MISSING,
    "terminal_code": MISSING,
    "terminal_status": MISSING,
    "cleanup_origin": MISSING,
    "cleanup_deadline": MISSING,
    "signal": MISSING,
    "signal_errno": 0,
    "guard_sample": MISSING,
    "remaining_sample": MISSING,
    "remaining": MISSING,
    "observer_result": MISSING,
    "wait_entered": False,
    "wait_allowance": MISSING,
    "returncode_before": MISSING,
    "returncode_after": MISSING,
    "wait_status": MISSING,
    "wait_exception": MISSING,
    "actual_reaped": False,
    "post_wait_sample": MISSING,
    "timeout_path": MISSING,
    "runner_code": MISSING,
}
_ROW: dict[str, Scalar] = {
    "attempt": 0,
    "stage": MISSING,
    "helper_origin": MISSING,
    "helper_deadline": MISSING,
    "visible": MISSING,
    "before_boot": MISSING,
    "before_forks": MISSING,
    "after_boot": MISSING,
    "after_forks": MISSING,
    "terminal_a": MISSING,
    "terminal_b": MISSING,
    "first_count": MISSING,
    "second_count": MISSING,
    "first_numeric": 0,
    "second_numeric": 0,
    "first_gone": 0,
    "second_gone": 0,
    "member_deadline_sample": MISSING,
    "final_sample": MISSING,
    "deadline_ok": MISSING,
    "counter_equal": MISSING,
    "leader_present": MISSING,
    "maps_equal": MISSING,
    "members_terminal": MISSING,
    "member_checked": 0,
    "offender_state": MISSING,
    "offender_threads": MISSING,
    "exception": MISSING,
    "errno": 0,
    "error_code": MISSING,
    "result": MISSING,
    "reason": MISSING,
}
_ERRORS = frozenset(
    {
        "native_observation_limit",
        "native_process_identity",
        "native_process_fields",
        "native_fork_counter",
        "native_session_identity",
        "native_observation_deadline",
    }
)


def loaded_paths() -> dict[str, str]:
    """Copy current module filenames without IO or another import."""
    return {
        name: filename
        for name, module in tuple(sys.modules.items())
        if isinstance(filename := getattr(module, "__file__", None), str)
    }


def loaded_hashes(paths: dict[str, str]) -> dict[str, dict[str, str]]:
    """Hash recorded module files only after timed supervision unwinds."""
    return {
        name: {
            "path": filename,
            "sha256": hashlib.sha256(Path(filename).read_bytes()).hexdigest(),
        }
        for name, filename in paths.items()
        if Path(filename).is_file()
    }


@dataclass
class Trace:
    """Preallocate first-eight/last-fifty-six records before execution."""

    meta: dict[str, Scalar] = field(default_factory=lambda: dict(_META))
    records: list[dict[str, Scalar]] = field(
        default_factory=lambda: [dict(_ROW) for _ in range(64)]
    )
    count: int = 0
    cursor: int = 0
    active: int = -1
    incomplete: bool = False
    reasons: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_REASONS, 0)
    )
    stdout: bytes = b""
    stderr: bytes = b""
    loaded: dict[str, str] = field(default_factory=loaded_paths)


@dataclass(frozen=True)
class Token:
    """Retain only diagnostic state needed for unconditional restoration."""

    previous: Trace | None
    trace: Trace


def current() -> Trace | None:
    """Read current thread-local state without creating a lock or thread."""
    value = getattr(_LOCAL, "trace", None)
    return value if isinstance(value, Trace) else None


def begin() -> Token:
    """Allocate the fixed diagnostic store before the original timed call."""
    token = Token(current(), Trace())
    _LOCAL.trace = token.trace
    return token


def restore(token: Token) -> None:
    """Restore prior thread-local state without export or other IO."""
    _LOCAL.trace = token.previous


def active(pgid: int) -> bool:
    """Select only the known owned leader in the existing worker thread."""
    trace = current()
    return trace is not None and trace.meta["pid"] == pgid


def mark[T: Scalar](name: str, value: T) -> T:
    """Copy a lifecycle scalar while returning the exact original operand."""
    trace = current()
    if trace is not None and name in trace.meta:
        trace.meta[name] = value
    return value


def first(name: str, value: Scalar) -> None:
    """Retain the first already sampled lifecycle value without a clock."""
    trace = current()
    if trace is not None and trace.meta.get(name) == MISSING:
        trace.meta[name] = value


def increment_meta(name: str) -> None:
    """Count an original lifecycle operation using a fixed scalar slot."""
    trace = current()
    if trace is not None:
        value = trace.meta.get(name)
        if type(value) is int:
            trace.meta[name] = min(MAX_COUNT, value + 1)


def outer_error(exception: Exception) -> None:
    """Distinguish an actual wait error from a final cleanup refusal."""
    trace = current()
    if (
        trace is not None
        and trace.meta["stage"] == "signal"
        and trace.meta["signal"] == MISSING
    ):
        trace.meta["signal"] = "other_errno"
        errno = getattr(exception, "errno", None)
        trace.meta["signal_errno"] = errno if type(errno) is int else 0
    if trace is not None and trace.meta["stage"] == "wait":
        trace.meta["wait_exception"] = (
            "timeout"
            if type(exception).__name__ == "TimeoutExpired"
            else "oserror"
        )


def put[T: Scalar](name: str, value: T) -> T:
    """Copy one already evaluated operand into its preallocated record."""
    trace = current()
    if trace is not None and trace.active >= 0 and name in _ROW:
        trace.records[trace.active][name] = value
    return value


def attempt(pgid: int) -> None:
    """Reuse a fixed record only for this thread's owned process group."""
    trace = current()
    if trace is None or not active(pgid):
        return
    trace.count = min(MAX_COUNT, trace.count + 1)
    index = trace.count - 1 if trace.count <= 8 else 8 + trace.cursor
    if trace.count > 8:
        trace.cursor = (trace.cursor + 1) % 56
    trace.active = index
    row = trace.records[index]
    for name, value in _ROW.items():
        row[name] = value
    row["attempt"] = trace.count
    trace.incomplete |= trace.count > 64


def increment(name: str) -> None:
    """Increment an existing count without allocating per-entry records."""
    trace = current()
    if trace is not None and trace.active >= 0:
        row = trace.records[trace.active]
        value = row.get(name)
        if type(value) is int:
            row[name] = min(MAX_COUNT, value + 1)


def member_stage() -> str:
    """Read an existing stage marker without inspecting another process."""
    trace = current()
    if (
        trace is not None
        and trace.active >= 0
        and trace.records[trace.active]["stage"] == "members_second"
    ):
        return "second"
    return "first"


def counter(name: str, value: tuple[int, int]) -> tuple[int, int]:
    """Retain the actual native boot/fork pair without another read."""
    put(name + "_boot", value[0])
    put(name + "_forks", value[1])
    return value


def view[T](name: str, value: dict[int, T]) -> dict[int, T]:
    """Record only the size of the returned already completed view."""
    put(name + "_count", len(value))
    return value


def member(state: str, threads: int) -> bool:
    """Preserve the original terminal predicate and first-failure iteration."""
    increment("member_checked")
    terminal = state in {"Z", "X"}
    result = terminal and threads == 1
    if not result:
        put("offender_state", "terminal" if terminal else "nonterminal")
        put("offender_threads", threads)
    return result


def error(exception: BaseException) -> None:
    """Record fixed error enums and errno without exception text."""
    name = type(exception).__name__
    put(
        "exception",
        (
            name
            if name
            in {
                "OSError",
                "ValueError",
                "KeyError",
                "TimeoutError",
                "PermissionError",
                "FileNotFoundError",
                "ProcessLookupError",
                "ChildProcessError",
            }
            else "other"
        ),
    )
    errno = getattr(exception, "errno", None)
    put("errno", errno if type(errno) is int else 0)
    code = exception.args[0] if exception.args else None
    put(
        "error_code",
        code if isinstance(code, str) and code in _ERRORS else "native_error",
    )


def finish(result: bool) -> bool:
    """Retain real results and only visited refusal operands."""
    put("result", result)
    trace = current()
    if trace is not None and trace.active >= 0:
        row = trace.records[trace.active]
        reason = "accepted" if result else "unknown"
        if row["exception"] != MISSING:
            reason = "exception"
        elif not result:
            for key in (
                "visible",
                "terminal_a",
                "terminal_b",
                "deadline_ok",
                "counter_equal",
                "leader_present",
                "maps_equal",
                "members_terminal",
            ):
                if row[key] is False:
                    reason = key
                    break
        put("reason", reason)
        trace.reasons[reason] = min(MAX_COUNT, trace.reasons[reason] + 1)
    return result


def capture(stdout: bytes, stderr: bytes) -> None:
    """Reuse bytes from the original uncapped reads without another read."""
    trace = current()
    if trace is not None:
        trace.stdout, trace.stderr = stdout, stderr


def document(trace: Trace) -> bytes:
    """Encode only outside supervision and refuse oversized diagnostics."""
    first = trace.records[: min(8, trace.count)]
    tail = trace.records[8 : min(64, trace.count)]
    if trace.count > 64:
        tail = tail[trace.cursor :] + tail[: trace.cursor]
    data = {
        "meta": trace.meta,
        "attempts": first + tail,
        "attempt_count": trace.count,
        "dropped_attempts": max(0, trace.count - 64),
        "incomplete": trace.incomplete,
        "reason_counts": trace.reasons,
        "stdout_bytes": len(trace.stdout),
        "stderr_bytes": len(trace.stderr),
    }
    encoded = json.dumps(data, separators=(",", ":")).encode()
    if len(encoded) > LIMIT:
        encoded = json.dumps(
            {
                "incomplete": True,
                "reason": "encoding_limit",
                "attempt_count": trace.count,
            }
        ).encode()
    return encoded


def export(trace: Trace) -> None:
    """Write raw output and bounded metadata after capture."""
    destination = Path(os.environ["MUNINN_CLEANUP_OBSERVATION_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("module.stdout", trace.stdout),
        ("module.stderr", trace.stderr),
        ("trace.json", document(trace)),
    ):
        with (destination / name).open("xb") as output:
            output.write(data)
    with (destination / "parent-loaded.json").open("x") as output:
        json.dump(
            {
                "before_supervision_paths_hashed_after_unwind": (
                    loaded_hashes(trace.loaded)
                ),
                "after_supervision": loaded_hashes(loaded_paths()),
                "child_loaded_closure": "not_observed",
            },
            output,
        )


def export_failure(exception: Exception) -> None:
    """Report an artifact error without replacing runner output."""
    errno = getattr(exception, "errno", None)
    if len(_EXPORT_ERRORS) < 16:
        _EXPORT_ERRORS.append(errno if type(errno) is int else 0)
    with contextlib.suppress(Exception):
        print(
            json.dumps(
                {
                    "code": "cleanup_observation_artifact_error",
                    "errno": _EXPORT_ERRORS[-1],
                }
            ),
            file=sys.stderr,
            flush=True,
        )
