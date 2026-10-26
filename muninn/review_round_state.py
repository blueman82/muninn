"""Hold bounded in-memory review rounds between durable transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from muninn.review_models import ReviewContext
from muninn.review_process import RunningProcess
from muninn.review_runner import LaunchSpec


@dataclass
class Round:
    """Hold one outstanding permit through durable terminal reconciliation."""

    context: ReviewContext
    initial: object
    knowledge: object
    history: list[tuple[object, object]] = field(
        default_factory=list[tuple[object, object]]
    )
    prompt: bytes = b""
    launch: LaunchSpec | None = None
    initialized: bool = False
    spawned: bool = False
    handle: RunningProcess | None = None
