"""Identify correctable schema failures without retaining model content."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import StrictModel


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
