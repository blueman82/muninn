"""Separate legacy host approval from an explicit typed managed host grant."""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from pydantic import ValidationError

from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tests.managed_codex_support import selection
from tests.test_review_codex_production import experimental_policy


class ManagedHostModelTests(TestCase):
    """Keep old bytes exact and reject crossed authority variants."""

    def test_typed_host_grant_accepts_only_managed_selection(self) -> None:
        old = experimental_policy(Path("/private/fixture"))
        assert old.experimental_codex is not None
        terms = old.experimental_codex.model_dump() | {
            "kind": "ongoing-mac-codex-managed-v1",
            "host_identity": {
                "kind": "darwin-ioplatformuuid-v1",
                "digest": "a" * 64,
            },
        }
        candidate = old.model_dump() | {
            "runner": selection(Path("/fixture/manager")).model_dump(),
            "experimental_codex": terms,
        }
        checked = ReviewPolicy.model_validate(candidate)
        assert checked.experimental_codex is not None
        self.assertEqual(checked.experimental_codex.kind, terms["kind"])
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate(candidate | {"runner": old.runner})

    def test_managed_selection_cannot_reuse_legacy_host_terms(self) -> None:
        old = experimental_policy(Path("/private/fixture"))
        with self.assertRaises(ValidationError):
            ReviewPolicy.model_validate(
                old.model_dump()
                | {"runner": selection(Path("/fixture/manager"))}
            )

    def test_legacy_policy_roundtrip_keeps_its_exact_projection(self) -> None:
        old = experimental_policy(Path("/private/fixture"))
        raw = canonical_json(old)
        self.assertEqual(
            canonical_json(ReviewPolicy.model_validate_json(raw)), raw
        )
        self.assertNotIn("darwin-ioplatformuuid-v1", raw)
