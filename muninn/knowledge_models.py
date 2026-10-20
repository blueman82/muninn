"""Define strict knowledge content and flat review result contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


def nonempty_text(value: str) -> str:
    """Reject empty human text without changing its source wording."""
    if not value.strip():
        raise ValueError("empty_text")
    return value


def schema_version(value: object) -> object:
    """Reject booleans and floats that compare equal to integer one."""
    if type(value) is not int:
        raise ValueError("invalid_schema_version")
    return value


def unique_evidence(value: list[str]) -> list[str]:
    """Reject repeated evidence references."""
    if len(value) != len(set(value)):
        raise ValueError("duplicate_evidence")
    return value


type Text = Annotated[str, AfterValidator(nonempty_text)]
type SchemaVersion = Annotated[Literal[1], BeforeValidator(schema_version)]
type KnowledgeId = Annotated[str, Field(pattern=r"^K_[0-9a-f]{32}$")]
type RequestId = Annotated[str, Field(pattern=r"^R_[0-9a-f]{32}$")]
type AttemptId = Annotated[str, Field(pattern=r"^A_[0-9a-f]{32}$")]
type EvidenceId = Annotated[str, Field(pattern=r"^E_[0-9a-f]{32}$")]
type UnresolvedId = Annotated[str, Field(pattern=r"^U_[0-9a-f]{32}$")]
type Revision = Annotated[int, Field(gt=0)]
type Provider = Literal["claude", "codex", "cursor"]
type Kind = Literal[
    "decision", "fact", "preference", "procedure", "lesson", "constraint"
]
type UtcSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type EvidenceRefs = Annotated[
    list[EvidenceId], Field(min_length=1), AfterValidator(unique_evidence)
]


class StrictModel(BaseModel):
    """Reject coercion and extra fields, and freeze model attributes."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Scope(StrictModel):
    """Identify a mapped project key and optional task."""

    project_id: Text | None
    task: Text | None

    @model_validator(mode="after")
    def global_task(self) -> Self:
        """Keep task scope inside a project; authority is checked later."""
        if self.project_id is None and self.task is not None:
            raise ValueError("global_task")
        return self


class Expiry(StrictModel):
    """Carry supported expiry conditions without a default lifetime."""

    at: UtcSeconds | None
    condition: Text | None
    evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def supported_limit(self) -> Self:
        """Require a time or condition and explicit support references."""
        if self.at is None and self.condition is None:
            raise ValueError("missing_expiry_limit")
        return self


class SupportedClaim(StrictModel):
    """Pair one assertion with unique, nonempty support references."""

    text: Text
    evidence_refs: EvidenceRefs


class NoteBody(StrictModel):
    """Represent one supported assertion and its explicit conditions."""

    form: Literal["note"]
    kind: Kind
    scope: Scope
    content: Text
    conditions: list[Text]
    evidence_refs: EvidenceRefs
    reason: Text


class GuideStep(StrictModel):
    """Represent a supported step with preceding dependencies."""

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    claim: SupportedClaim
    depends_on: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]]


class GuideBody(StrictModel):
    """Represent supported guidance with an ordered dependency graph."""

    form: Literal["guide"]
    kind: Kind
    scope: Scope
    title: Text
    summary: SupportedClaim
    applicability: list[SupportedClaim]
    steps: Annotated[list[GuideStep], Field(min_length=1)]
    decision_points: list[SupportedClaim]
    pitfalls: list[SupportedClaim]
    reason: Text

    @model_validator(mode="after")
    def preceding_dependencies(self) -> Self:
        """Reject repeated keys, forward references and cycles."""
        preceding: set[str] = set()
        for step in self.steps:
            if step.key in preceding or not set(step.depends_on) <= preceding:
                raise ValueError("invalid_step_dependencies")
            preceding.add(step.key)
        return self


class AddNote(NoteBody):
    """Propose a note without allocating lifecycle values."""

    action: Literal["add"]


class AddGuide(GuideBody):
    """Propose a guide without allocating lifecycle values."""

    action: Literal["add"]


class ReplaceNote(NoteBody):
    """Replace a referenced note revision using optimistic concurrency."""

    action: Literal["replace"]
    knowledge_id: KnowledgeId
    expected_revision: Revision


class ReplaceGuide(GuideBody):
    """Replace a referenced guide revision using optimistic concurrency."""

    action: Literal["replace"]
    knowledge_id: KnowledgeId
    expected_revision: Revision


type Add = Annotated[AddNote | AddGuide, Field(discriminator="form")]
type Replace = Annotated[
    ReplaceNote | ReplaceGuide, Field(discriminator="form")
]
type Change = Annotated[Add | Replace, Field(discriminator="action")]
type Body = Annotated[NoteBody | GuideBody, Field(discriminator="form")]


class AffectedRevision(StrictModel):
    """Identify an existing knowledge revision affected by a proposal."""

    knowledge_id: KnowledgeId
    revision: Revision


class UnresolvedProposal(StrictModel):
    """Ask a request-bound question whose authority is validated later."""

    claim: Text
    affected: list[AffectedRevision]
    evidence_refs: Annotated[list[EvidenceId], AfterValidator(unique_evidence)]
    question: Text


class ReviewResult(StrictModel):
    """Contain proposals only, without lifecycle or save claims."""

    schema_version: SchemaVersion
    changes: list[Change]
    unresolved: list[UnresolvedProposal]
    no_change_reason: Text | None

    @model_validator(mode="after")
    def empty_reason(self) -> Self:
        """Require a no-change reason exactly when proposals are empty."""
        empty = not self.changes and not self.unresolved
        if empty != (self.no_change_reason is not None):
            raise ValueError("invalid_no_change_reason")
        return self


class KnowledgeView(StrictModel):
    """Present program-owned knowledge state and content."""

    schema_version: SchemaVersion
    id: KnowledgeId
    revision: Revision
    state: Literal["active", "withheld", "retracted", "erased"]
    body: Body | None
    origin: Literal["review", "proposal"]
    expiry: Expiry | None
    created_at: UtcSeconds
    retracted_at: UtcSeconds | None
    retracted_reason: Text | None

    @model_validator(mode="after")
    def content_state(self) -> Self:
        """Preserve retained content and clear erased content and reasons."""
        if self.state == "erased":
            if self.body is not None or self.retracted_reason is not None:
                raise ValueError("erased_content")
        elif self.body is None:
            raise ValueError("missing_retained_content")
        if self.state == "retracted" and (
            self.retracted_at is None or self.retracted_reason is None
        ):
            raise ValueError("missing_retraction")
        return self


class GuideIndex(StrictModel):
    """Present a guide label without exposing uncited body assertions."""

    id: KnowledgeId
    revision: Revision
    title: Text
    scope: Scope


class KnowledgeContext(StrictModel):
    """Present eligible notes and a separate guide index."""

    schema_version: SchemaVersion
    notes: list[KnowledgeView]
    guides: list[GuideIndex]
    unresolved_ids: list[UnresolvedId]
    truncated: bool

    @model_validator(mode="after")
    def note_bodies(self) -> Self:
        """Require active note bodies; runtime checks current eligibility."""
        if any(
            note.state != "active" or not isinstance(note.body, NoteBody)
            for note in self.notes
        ):
            raise ValueError("ineligible_context_note")
        return self
