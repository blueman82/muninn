"""Retain finite lifecycle events while independently containing wrappers."""

from __future__ import annotations

import contextlib
import os
import signal
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from muninn.knowledge_models import AttemptId
from muninn.review_codex_output import decode_codex_output, tool_event_seen
from muninn.review_models import (
    ObservedConfig,
    ProcessIdentity,
    ReviewPolicy,
    Usage,
)
from muninn.review_output import decode_claude_output
from muninn.review_process_identity import identify, sole_zombie
from muninn.review_process_protocol import (
    CONTROL_HEADER_MAX,
    LaunchNotice,
    TerminalNotice,
    load,
)
from muninn.review_process_quiescence import group_quiescent


@dataclass(frozen=True)
class RunningProcess:
    """Retain one unreaped wrapper and its established native identity."""

    attempt_id: AttemptId
    round_index: int
    wrapper: subprocess.Popen[bytes]
    identity: ProcessIdentity
    deadline: float
    output_limit: int


@dataclass(frozen=True)
class WorkerReady:
    """Report a verified wrapper before durable identity and activation."""

    attempt_id: AttemptId
    round_index: int
    identity: ProcessIdentity
    kind: Literal["ready"] = "ready"


@dataclass(frozen=True)
class WorkerLaunched:
    """Report actual CLI creation separately from parent activation."""

    attempt_id: AttemptId
    round_index: int
    launched_at: float
    cli_identity: ProcessIdentity
    kind: Literal["launched"] = "launched"


@dataclass(frozen=True)
class ProcessOutcome:
    """Return bounded decoded bytes only after wrapper containment and reap."""

    attempt_id: AttemptId
    round_index: int
    stdout: bytes
    exit_code: int | None
    error_code: str | None
    elapsed_seconds: float
    observed: ObservedConfig
    usage: Usage | None
    kind: Literal["outcome"] = "outcome"


type ProcessEvent = WorkerReady | WorkerLaunched | ProcessOutcome


@dataclass
class _Slot:
    """Retain at most one event per phase and bounded pipe state."""

    handle: RunningProcess
    began: float = field(default_factory=time.monotonic)
    ready: WorkerReady | None = None
    launched: WorkerLaunched | None = None
    terminal: ProcessOutcome | None = None
    activation: bytes | None = None
    configured: bool = False
    activated: bool = False
    error: str | None = None
    phase: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    output: bytearray = field(default_factory=bytearray)
    output_count: int = 0
    write_at: int = 0
    exit_code: int | None = None
    finished: bool = False


def contain(handle: RunningProcess) -> int:
    """Signal the verified group before every wrapper wait or hidden poll."""
    proc = handle.wrapper
    if os.name == "nt":
        proc.kill()
    else:
        # Exclusive unreaped-child ownership reserves the verified PGID even
        # when native creation queries no longer expose the zombie leader.
        if (
            handle.identity.pid != proc.pid
            or handle.identity.pgid != proc.pid
            or proc.returncode is not None
        ):
            raise OSError("process_identity_unverified")
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if not sole_zombie(proc.pid):
                raise
        if not group_quiescent(proc.pid):
            raise subprocess.TimeoutExpired(proc.args, 0.01)
    result = proc.wait(timeout=0.01)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
    return result


class ProcessMonitor:
    """Own process pipes and cleanup without SQLite or process creation."""

    def __init__(
        self,
        limits: ReviewPolicy,
        *,
        runner: Literal["claude_cli", "codex_cli"] = "claude_cli",
    ) -> None:
        """Start one independent monitor with explicit retained capacity."""
        self.limits = limits
        self._native_codex = runner == "codex_cli"
        self._decode = (
            decode_codex_output
            if runner == "codex_cli"
            else decode_claude_output
        )
        self.lock = threading.Lock()
        self.slots: dict[tuple[str, int], _Slot] = {}
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def register(self, handle: RunningProcess) -> None:
        """Adopt the caller's reserved handle before readiness validation."""
        key = (handle.attempt_id, handle.round_index)
        slot = _Slot(handle)
        with self.lock:
            if key in self.slots or len(self.slots) >= self.limits.concurrency:
                raise ValueError("monitor_capacity")
            self.slots[key] = slot
        try:
            if handle.wrapper.stdin is None or handle.wrapper.stdout is None:
                raise ValueError("worker_pipes")
            os.set_blocking(handle.wrapper.stdin.fileno(), False)
            os.set_blocking(handle.wrapper.stdout.fileno(), False)
        except (OSError, ValueError):
            slot.error = "worker_pipes"
        with self.lock:
            slot.configured = True

    def activate(
        self, attempt_id: AttemptId, round_index: int, frame: bytes
    ) -> None:
        """Transfer one bounded frame for monitor-owned nonblocking writes."""
        if len(frame) > 12 + CONTROL_HEADER_MAX + self.limits.max_input_bytes:
            raise ValueError("activation_limit")
        with self.lock:
            slot = self.slots.get((attempt_id, round_index))
            if (
                slot is None
                or slot.phase != 1
                or slot.activated
                or slot.terminal is not None
            ):
                raise ValueError("invalid_worker_phase")
            slot.activation = frame
            slot.activated = True

    def drain(self) -> tuple[ProcessEvent, ...]:
        """Transfer ordered ready, launched and cleaned terminal references."""
        events: list[ProcessEvent] = []
        with self.lock:
            for key, slot in tuple(self.slots.items()):
                events.extend(
                    event
                    for event in (slot.ready, slot.launched, slot.terminal)
                    if event is not None
                )
                slot.ready = slot.launched = None
                if slot.terminal is not None:
                    del self.slots[key]
        return tuple(events)

    def stop_all(self) -> None:
        """Close parent control and contain all wrappers on the monitor."""
        with self.lock:
            self._stopping = True
        self._thread.join(timeout=self.limits.deadline_seconds)
        if self._thread.is_alive():
            raise OSError("process_cleanup_incomplete")

    def _run(self) -> None:
        """Service each bounded slot independently of blocked main writes."""
        while True:
            with self.lock:
                slots = tuple(self.slots.values())
                stopping = self._stopping
            for slot in slots:
                if not slot.configured or slot.terminal is not None:
                    continue
                try:
                    self._step(slot, stopping)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RecursionError,
                ):
                    self._finish(slot, "invalid_worker_protocol")
            if stopping and all(s.terminal is not None for s in slots):
                return
            time.sleep(0.01)

    def _step(self, slot: _Slot, stopping: bool) -> None:
        """Exchange bounded pipe chunks without holding the state mutex."""
        if slot.finished:
            self._finish(slot, slot.error)
            return
        with self.lock:
            error, activation = slot.error, slot.activation
        if stopping or error or time.monotonic() >= slot.handle.deadline:
            self._finish(
                slot, error or ("stopped" if stopping else "deadline")
            )
            return
        proc = slot.handle.wrapper
        assert proc.stdin is not None and proc.stdout is not None
        if activation is not None and slot.write_at < len(activation):
            with contextlib.suppress(BlockingIOError):
                slot.write_at += os.write(
                    proc.stdin.fileno(),
                    activation[slot.write_at : slot.write_at + 4096],
                )
            if slot.write_at == len(activation):
                with self.lock:
                    slot.activation = None
        try:
            raw = os.read(proc.stdout.fileno(), 4096)
        except BlockingIOError:
            return
        if not raw:
            self._finish(slot, "worker_eof")
            return
        slot.buffer.extend(raw)
        self._frames(slot)
        if slot.finished:
            self._finish(slot, slot.error)

    def _frames(self, slot: _Slot) -> None:
        """Bound each header before accumulation and consume finite phases."""
        while len(slot.buffer) >= 5:
            tag = bytes(slot.buffer[:1])
            size = struct.unpack("!I", slot.buffer[1:5])[0]
            limit = (
                slot.handle.output_limit
                if tag in (b"O", b"E")
                else CONTROL_HEADER_MAX
            )
            if size > limit:
                slot.error, slot.finished = "output_limit", True
                return
            if len(slot.buffer) < size + 5:
                return
            body = bytes(slot.buffer[5 : 5 + size])
            del slot.buffer[: 5 + size]
            self._frame(slot, tag, body)
            if self._native_codex and slot.error is not None:
                return

    def _output(self, slot: _Slot, tag: bytes, body: bytes) -> None:
        """Count aggregate native bytes while discarding stderr content."""
        if slot.phase != 2:
            raise ValueError("invalid_worker_phase")
        slot.output_count += len(body)
        # Inspect the bounded incoming frame before discarding an overflow;
        # its complete event may report an action that already occurred.
        if (
            self._native_codex
            and tag == b"O"
            and tool_event_seen(bytes(slot.output) + body)
        ):
            slot.error, slot.finished = "native_tool_event", True
        elif slot.output_count > slot.handle.output_limit:
            slot.error, slot.finished = "output_limit", True
        elif tag == b"O":
            slot.output.extend(body)

    def _frame(self, slot: _Slot, tag: bytes, body: bytes) -> None:
        """Validate lifecycle identity separately from untrusted CLI bytes."""
        handle = slot.handle
        if slot.finished:
            raise ValueError("duplicate_terminal")
        if tag in (b"O", b"E"):
            self._output(slot, tag, body)
            return
        value = load(body)
        if not isinstance(value, dict):
            raise ValueError("invalid_worker_protocol")
        if tag == b"R" and slot.phase == 0:
            ready = ProcessIdentity.model_validate(value)
            if ready != handle.identity or ready != identify(
                handle.wrapper.pid
            ):
                raise ValueError("process_identity_unverified")
            if os.name != "nt" and ready.pgid != ready.pid:
                raise ValueError("process_identity_unverified")
            event = WorkerReady(handle.attempt_id, handle.round_index, ready)
            with self.lock:
                slot.ready, slot.phase = event, 1
        elif tag == b"L" and slot.phase == 1 and slot.activated:
            notice = LaunchNotice.model_validate_json(body)
            if os.name != "nt" and notice.identity.pgid != handle.wrapper.pid:
                raise ValueError("process_identity_unverified")
            launched = WorkerLaunched(
                handle.attempt_id,
                handle.round_index,
                notice.launched_at,
                notice.identity,
            )
            with self.lock:
                slot.launched, slot.phase = launched, 2
        elif tag == b"T" and slot.phase == 2:
            notice = TerminalNotice.model_validate_json(body)
            slot.exit_code, slot.finished = notice.exit_code, True
            slot.error = "worker_failed" if slot.exit_code else None
        else:
            raise ValueError("invalid_worker_phase")

    def _finish(self, slot: _Slot, error: str | None) -> None:
        """Contain before publishing the single retained terminal outcome."""
        if slot.terminal is not None:
            return
        try:
            contain(slot.handle)
        except subprocess.TimeoutExpired:
            # A killed wrapper can still be exiting; retry without changing
            # the outcome that its terminal frame or prior error established.
            with self.lock:
                slot.finished = True
                if error is not None:
                    slot.error = error
            return
        except OSError:
            with self.lock:
                if error is not None:
                    slot.error = error
            return
        stdout = b""
        observed = ObservedConfig(
            runner=None, version=None, model=None, reasoning=None
        )
        usage = None
        if error is None:
            try:
                stdout, observed, usage = self._decode(
                    bytes(slot.output), slot.handle.output_limit
                )
            except ValueError as exc:
                error = (
                    "native_tool_event"
                    if str(exc) == "native_tool_event"
                    else "invalid_native_output"
                )
        outcome = ProcessOutcome(
            slot.handle.attempt_id,
            slot.handle.round_index,
            stdout,
            slot.exit_code,
            error,
            time.monotonic() - slot.began,
            observed,
            usage,
        )
        slot.output.clear()
        slot.buffer.clear()
        with self.lock:
            slot.activation = None
            slot.terminal = outcome


def cancel_registered(
    monitor: ProcessMonitor, attempt_id: AttemptId, round_index: int, code: str
) -> None:
    """Request cleanup through one bounded state exchange."""
    with monitor.lock:
        slot = monitor.slots.get((attempt_id, round_index))
        if slot is not None and slot.terminal is None:
            slot.error = code


def adopt_after_registration_error(
    monitor: ProcessMonitor, handle: RunningProcess
) -> None:
    """Keep failed adoption serviceable without touching pipes or capacity."""
    key = (handle.attempt_id, handle.round_index)
    with monitor.lock:
        slot = monitor.slots.get(key)
        if slot is None:
            if len(monitor.slots) >= monitor.limits.concurrency:
                raise RuntimeError("monitor_ownership_invariant")
            slot = _Slot(handle)
            monitor.slots[key] = slot
        elif slot.handle is not handle:
            raise RuntimeError("monitor_ownership_invariant")
        if slot.terminal is None:
            slot.error, slot.configured = "registration_failed", True
