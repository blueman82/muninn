"""Typed selectors for existing legacy installer recovery records."""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import (
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    with_config,
)
from typing_extensions import TypedDict

from install.context import StepFailedError

LegacyPhase = Literal[
    "intent",
    "prepared",
    "launcher_intent",
    "launcher",
    "runtime_intent",
    "runtime",
    "service_intent",
    "service",
    "armed",
    "verified",
    "recovery_required",
    "publication_possible",
    "published",
    "configuring",
    "configured",
    "launcher_possible",
    "activated",
    "starting",
    "complete",
]


@with_config(ConfigDict(strict=True, extra="allow"))
class LegacyHeader(TypedDict):
    """Selectors whose native bindings remain independently verified."""

    version: int
    baseline: str
    platform: str
    phase: LegacyPhase
    publication: Literal["not_started", "possible", "complete"]
    # Context-dependent checks retain their own refusal codes and ordering.
    record_directory: NotRequired[object]
    publication_binding: NotRequired[object]


@with_config(ConfigDict(strict=True, extra="allow"))
class RecoveryContext(TypedDict):
    """Saved operation fields required before selecting a recovery context."""

    ts: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    home: str
    rdir: str
    fresh: bool
    upgrade: bool
    legacy: LegacyHeader


_CONTEXT = TypeAdapter(RecoveryContext)


def context(value: object) -> RecoveryContext:
    """Validate selectors without replacing the original mutable record."""
    try:
        return _CONTEXT.validate_python(value)
    except ValidationError:
        raise StepFailedError("legacy_record_invalid") from None
