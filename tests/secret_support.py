"""Share one secret marker and one marker-bearing validation error."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError, field_validator

SECRET_MARKER = "SECRET-MARKER-7f3a"


class _Model(BaseModel):
    """Raise a validation error that carries the secret in its text."""

    value: int

    @field_validator("value")
    @classmethod
    def _leak(cls, value: int) -> int:
        """Always reject the value, with the marker in the message."""
        raise ValueError(SECRET_MARKER)


def validation_error() -> ValidationError:
    """Build a real pydantic error whose text holds the marker."""
    try:
        _Model(value=1)
    except ValidationError as exc:
        return exc
    raise AssertionError("validator did not raise")
