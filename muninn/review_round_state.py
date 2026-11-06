"""Hold bounded in-memory review rounds between durable transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from muninn.review_correction import CorrectionState
from muninn.review_models import ReviewContext
from muninn.review_process import RunningProcess, WorkerReady
from muninn.review_runner import LaunchSpec
from muninn.review_runtime_models import RuntimeIdentity


@dataclass
class Round:
    """Hold one outstanding permit through durable terminal reconciliation."""

    context: ReviewContext
    initial: object
    knowledge: object
    history: list[tuple[object, object]] = field(
        default_factory=list[tuple[object, object]]
    )
    correction: CorrectionState | None = None
    prompt: bytes = b""
    launch: LaunchSpec | None = None
    initialized: bool = False
    spawned: bool = False
    handle: RunningProcess | None = None
    refresh_count: Literal[0, 1] = 0
    first_execution: RuntimeIdentity | None = None
    prepared_prompt: bytes = b""
    ready: WorkerReady | None = None
