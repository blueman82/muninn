"""Errors raised by the provider-config editors."""

from __future__ import annotations


class RefusedError(Exception):
    """Unexpected layout or unproven edit; nothing was written."""


class RacedError(Exception):
    """The file kept changing under us; our edit was not applied."""
