"""Define data-only evidence provenance and projection contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from muninn.knowledge_models import (
    Digest,
    EvidenceId,
    Provider,
    Scope,
    StrictModel,
    Text,
    UtcSeconds,
)

type PositiveInt = Annotated[int, Field(gt=0)]
type NonnegativeInt = Annotated[int, Field(ge=0)]
type EvidenceType = Literal[
    "owner_statement",
    "assistant_report",
    "tool_request",
    "tool_result",
    "observed_outcome",
]
type Authority = Literal["direct_owner", "reported", "untrusted"]


class SourceRef(StrictModel):
    """Identify one provider thread, independent of filesystem layout."""

    provider: Provider
    thread_id: Text


class EvidenceWindow(StrictModel):
    """Describe inclusive complete-record boundaries of an admitted source."""

    source: SourceRef
    from_line: PositiveInt
    through_line: PositiveInt
    boundary_hash: Digest

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        """Reject inverted inclusive source boundaries."""
        if self.from_line > self.through_line:
            raise ValueError("inverted_window")
        return self


class EvidenceLocator(StrictModel):
    """Identify one exact record and part without granting admission."""

    source: SourceRef
    line: PositiveInt
    part: PositiveInt
    line_sha256: Digest


class EvidenceSelector(StrictModel):
    """Select a decoded text field and optional half-open Unicode span."""

    json_pointer: str
    start: NonnegativeInt | None
    end: NonnegativeInt | None

    @model_validator(mode="after")
    def valid_span(self) -> Self:
        """Require paired ordered offsets and JSON pointer syntax."""
        if self.json_pointer and not self.json_pointer.startswith("/"):
            raise ValueError("invalid_json_pointer")
        if (self.start is None) != (self.end is None):
            raise ValueError("incomplete_span")
        if (
            self.start is not None
            and self.end is not None
            and self.start > self.end
        ):
            raise ValueError("inverted_span")
        for token in self.json_pointer.split("/")[1:]:
            if "~" in token.replace("~0", "").replace("~1", ""):
                raise ValueError("invalid_pointer_escape")
        return self


class JsonlOriginal(StrictModel):
    """Carry program-owned byte provenance for a JSONL record."""

    kind: Literal["jsonl"]
    path: Path
    byte_offset: NonnegativeInt
    byte_length: PositiveInt


class CursorOriginal(StrictModel):
    """Carry program-owned database provenance for a Cursor record."""

    kind: Literal["cursor_db"]
    database: Path
    conversation_id: Text
    record_key: Text


type OriginalLocator = Annotated[
    JsonlOriginal | CursorOriginal, Field(discriminator="kind")
]


class EvidenceRecord(StrictModel):
    """Pair admitted provenance with a selected evidence projection."""

    id: EvidenceId
    provider: Provider
    thread_id: Text
    session_root: Text
    line: PositiveInt
    part: PositiveInt
    line_sha256: Digest
    source_version: Text
    projection_version: Text
    original_locator: OriginalLocator
    selector: EvidenceSelector
    selector_digest: Digest
    evidence_type: EvidenceType
    authority: Authority
    scope: Scope
    excerpt: Text | None
    state: Literal["live", "changed", "erased"]
    selected_at: UtcSeconds

    @model_validator(mode="after")
    def erased_excerpt(self) -> Self:
        """Avoid retaining excerpt text on an erased evidence view."""
        if self.state == "erased" and self.excerpt is not None:
            raise ValueError("erased_excerpt")
        return self


class OutcomeLocator(StrictModel):
    """Carry program-owned outcome metadata without command or output text."""

    locator: EvidenceLocator
    evidence_type: EvidenceType
    status_code: Text | None


class InitialEvidence(StrictModel):
    """Carry bounded evidence chosen before the first model call."""

    excerpts: tuple[EvidenceRecord, ...]
    outcomes: tuple[OutcomeLocator, ...]
    truncated: bool
    bytes_used: NonnegativeInt
