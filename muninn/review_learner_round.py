"""Hold one learning round in memory between durable transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from muninn.review_correction import CorrectionState
from muninn.review_models import ReviewContext


@dataclass
class Round:
    """Hold one request's context, evidence and prompt across rounds."""

    context: ReviewContext
    initial: object
    knowledge: object
    history: list[tuple[object, object]] = field(
        default_factory=list[tuple[object, object]]
    )
    correction: CorrectionState | None = None
    prompt: bytes = b""
    initialized: bool = False
