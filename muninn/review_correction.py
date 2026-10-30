"""Define finite correction feedback and verify exact candidate removals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import ReviewResult, StrictModel
from muninn.review_json import canonical_json


class RoundFormatError(ValueError):
    """Identify a schema failure after bounded strict JSON decoding."""

    code: Literal["model_format"] = "model_format"

    def __init__(self) -> None:
        """Expose only the fixed format code to callers."""
        super().__init__(self.code)


class CorrectionFeedback(StrictModel):
    """Carry finite correction coordinates without raw diagnostic strings."""

    kind: Literal["format", "reduction"]
    code: Literal[
        "model_format",
        "candidate_scope",
        "owner_support",
        "duplicate_target",
        "target_overlap",
    ]
    collection: Literal["changes", "unresolved"] | None
    index: Annotated[int, Field(ge=0)] | None

    @model_validator(mode="after")
    def consistent_kind(self) -> Self:
        """Require reduction coordinates and a matching finite code."""
        if self.kind == "format":
            if (
                self.code != "model_format"
                or self.collection is not None
                or self.index is not None
            ):
                raise ValueError("invalid_format_feedback")
        elif (
            self.code == "model_format"
            or self.collection is None
            or self.index is None
        ):
            raise ValueError("invalid_reduction_feedback")
        return self


@dataclass(frozen=True)
class CorrectionState:
    """Retain finite feedback and an optional batch only in worker memory."""

    feedback: CorrectionFeedback
    original: ReviewResult | None


def _subsequence(original: list[str], corrected: list[str]) -> bool:
    """Consume exact canonical items once each in their original order."""
    remaining = iter(original)
    return all(
        any(item == candidate for candidate in remaining) for item in corrected
    )


def require_reduction(original: ReviewResult, corrected: ReviewResult) -> None:
    """Require an exact ordered subset with at least one candidate removed."""
    try:
        corrected = ReviewResult.model_validate_json(canonical_json(corrected))
    except ValueError:
        raise ValueError("rejected_validation") from None
    before = len(original.changes) + len(original.unresolved)
    after = len(corrected.changes) + len(corrected.unresolved)
    if after >= before or (
        after == 0
        and corrected.no_change_reason
        != "No supported candidates remain after correction."
    ):
        raise ValueError("rejected_validation")
    for previous, current in (
        (original.changes, corrected.changes),
        (original.unresolved, corrected.unresolved),
    ):
        if not _subsequence(
            [canonical_json(item) for item in previous],
            [canonical_json(item) for item in current],
        ):
            raise ValueError("rejected_validation")
