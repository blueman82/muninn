"""Validate policy subrecords without converting their saved JSON shapes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from install.context import Ctx, StepFailedError
from install.policy_record_models import (
    Activation,
    DefaultSelection,
    Intent,
    PolicyInput,
    Ready,
    RecoveryEnvelope,
    Selection,
)
from muninn.review_json import unique_object

INPUT = TypeAdapter(PolicyInput)
DEFAULT = TypeAdapter(DefaultSelection)
ACTIVATION = TypeAdapter(Activation)
INTENT = TypeAdapter(Intent)
READY = TypeAdapter(Ready)
SELECTION = TypeAdapter(Selection)
ENVELOPE = TypeAdapter(RecoveryEnvelope)
RECORD = TypeAdapter(dict[str, object])


def policy_input(value: object) -> PolicyInput:
    """Reject partial or wrongly typed input without exposing policy bytes."""
    try:
        return INPUT.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_policy_snapshot_changed") from None


def default_selection(value: object) -> DefaultSelection:
    """Require every default capability binding before constructing paths."""
    try:
        return DEFAULT.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_policy_default_changed") from None


def activation(value: object) -> Activation:
    """Require a complete activation intent and valid completion phase."""
    try:
        return ACTIVATION.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_activation_record_changed") from None


def intent(value: object) -> Intent:
    """Validate a live control snapshot before retaining it as authority."""
    try:
        return INTENT.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_activation_record_changed") from None


def selection(value: object) -> Selection:
    """Validate native link observations without exposing their values."""
    try:
        return SELECTION.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_activation_runtime_changed") from None


def ready(value: object, ctx: Ctx | None = None) -> Ready:
    """Require complete ready bindings and, when available, managed paths."""
    try:
        result = READY.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_activation_runtime_changed") from None
    if ctx is not None:
        if set(result["files"]) != {
            str(ctx.settings),
            str(ctx.config),
            str(ctx.cursor_settings),
        }:
            raise StepFailedError("review_activation_configuration_changed")
        if set(result["selection"]["links"]) != {
            str(ctx.lib / "current"),
            str(ctx.lib / "python"),
            str(ctx.muninn),
        }:
            raise StepFailedError("review_activation_runtime_changed")
    return result


def envelope(value: object) -> RecoveryEnvelope:
    """Validate recovery context fields before deriving any operation path."""
    try:
        result = ENVELOPE.validate_python(value)
    except ValidationError:
        raise StepFailedError("review_activation_record_changed") from None
    if result["fresh"] == result["upgrade"]:
        raise StepFailedError("review_activation_record_changed")
    return result


def validate(value: object) -> dict[str, object]:
    """Check present policy subrecords while retaining dynamic outer fields."""
    if not isinstance(value, dict):
        raise StepFailedError("legacy_record_invalid")
    try:
        result = RECORD.validate_python(value, strict=True)
    except ValidationError:
        raise StepFailedError("legacy_record_invalid") from None
    for name, check in (
        ("policy_input", policy_input),
        ("policy_activation", activation),
        ("policy_ready", ready),
    ):
        if name in result:
            check(result[name])
    return result


def parse(raw: str | bytes) -> dict[str, object]:
    """Reject ambiguous JSON while preserving opaque provider preimages."""
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise StepFailedError("legacy_record_invalid") from None
    return validate(value)


def default_source(value: Mapping[str, object]) -> DefaultSelection:
    """Validate the default fragment after separating run ownership fields."""
    return default_selection(
        {
            key: item
            for key, item in value.items()
            if key
            not in {
                "status",
                "operation",
                "home",
                "home_identity",
                "sha",
                "enable_learning",
            }
        }
    )


def activation_phase(
    rec: Mapping[str, object],
) -> Literal["intent", "complete"] | None:
    """Read an optional phase through the complete activation contract."""
    if "policy_activation" not in rec:
        return None
    return activation(rec["policy_activation"])["phase"]


def input_raw(rec: Mapping[str, object]) -> str | None:
    """Read optional selected input through its complete snapshot contract."""
    if "policy_input" not in rec:
        return None
    raw = policy_input(rec["policy_input"]).get("raw")
    return raw if isinstance(raw, str) else None


def learning_consent(rec: Mapping[str, object]) -> bool:
    """Read explicit restart approval through the strict input contract."""
    if "policy_input" not in rec:
        return False
    return policy_input(rec["policy_input"]).get("enable_learning", False)
