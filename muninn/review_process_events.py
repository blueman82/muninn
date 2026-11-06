"""Carry owned-process events without monitor or database dependencies."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Literal

from muninn.knowledge_models import AttemptId
from muninn.review_models import ObservedConfig, ProcessIdentity, Usage


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
    identity_digest: str | None = None


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


@dataclass(frozen=True)
class WorkerPrestartRefused:
    """Report a proved refusal from the owned wrapper before native exec."""

    attempt_id: AttemptId
    round_index: int
    identity_digest: str
    code: Literal["runtime_changed", "exec_enoent"]
    kind: Literal["prestart_refused"] = "prestart_refused"


type ProcessEvent = (
    WorkerReady | WorkerLaunched | WorkerPrestartRefused | ProcessOutcome
)


@dataclass
class ProcessSlot:
    """Retain at most one event per phase and bounded pipe state."""

    handle: RunningProcess
    began: float = field(default_factory=time.monotonic)
    ready: WorkerReady | None = None
    launched: WorkerLaunched | None = None
    refused: WorkerPrestartRefused | None = None
    managed: bool = False
    replacement: bool = False
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


def legacy_event(
    event: ProcessEvent,
) -> WorkerReady | WorkerLaunched | ProcessOutcome:
    """Reject managed-only transport at an exact legacy trial boundary."""
    if isinstance(event, WorkerPrestartRefused):
        raise ValueError("invalid_worker_protocol")
    return event
