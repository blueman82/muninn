"""Reject changed or expired offline subscription contracts before entry."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_learning_eval_account import account_assessment
from tools.learning_eval.cases import config_digest, digest


class SubscriptionTermsTests(unittest.TestCase):
    """Exercise declared authority without inspecting real authentication."""

    def setUp(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.subscription_terms"),
            "offline subscription contract is absent",
        )
        self.module = importlib.import_module(
            "tools.learning_eval.subscription_terms"
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "account"
        self.path.mkdir(mode=0o700)
        self.authority = self.root / "authority.json"
        self.authority.write_bytes(b'{"scope":"synthetic-offline-only"}')
        self.authority.chmod(0o600)
        assessment = account_assessment(
            max_model_calls=2,
            max_wall_seconds=600.0,
            max_cost_decimal=None,
            conservative_call_cost_decimal=None,
            currency=None,
        )
        runner = assessment.policies[0].runner.model_copy(
            update={
                "runner": "codex_cli",
                "executable": str(self.root / "never-executed"),
                "version": "synthetic-version",
                "model": "synthetic-model",
                "reasoning": "low",
            }
        )
        policy = assessment.policies[0].model_copy(update={"runner": runner})
        self.assessment = assessment.model_copy(update={"policies": (policy,)})
        self.assessment = self.assessment.model_copy(
            update={"config_digest": config_digest(self.assessment)}
        )
        self.data = {
            "schema_version": 1,
            "kind": "codex-subscription-characterization-v1",
            "assessment_id": self.assessment.id,
            "budget": self.assessment.budget.model_dump(mode="json"),
            "account_path": str(self.path),
            "receipt_path": str(self.root / "subscription-receipt.json"),
            "owner_boundary_path": str(self.authority),
            "owner_boundary_digest": hashlib.sha256(
                self.authority.read_bytes()
            ).hexdigest(),
            "started_at": 10.0,
            "deadline_at": 610.0,
            "per_call_seconds": 90.0,
            "candidate_digest": "a" * 64,
            "host_identity": "b" * 64,
            "auth_context_digest": "c" * 64,
            "executable_sha256": "d" * 64,
            "schema_digest": "e" * 64,
            "decoder_digest": "f" * 64,
            "config_digest": self.assessment.config_digest,
            "requested": runner.model_dump(mode="json"),
            "auth_method": "chatgpt",
            "purpose": "characterization_only",
        }
        self.terms = self.module.SubscriptionTerms.model_validate_json(
            json.dumps(self.data)
        )
        self.selection = self.module.SubscriptionSelection(
            terms_digest=digest(self.terms.model_dump(mode="json")),
            candidate_digest="a" * 64,
            host_identity="b" * 64,
            auth_context_digest="c" * 64,
            requested=runner,
            auth_method="chatgpt",
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX private authority reader")
    def test_null_money_validates_without_claiming_observed_usage(
        self,
    ) -> None:
        self.module.validate_terms(
            self.terms, self.assessment, self.selection, 10.0
        )
        self.assertIsNone(self.terms.budget.max_cost_decimal)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_schema_refuses_growth_import_paid_route_and_invalid_limits(
        self,
    ) -> None:
        mutations = [
            {"schema_version": True},
            {"kind": "native_codex_trial"},
            {"purpose": "C6"},
            {"auth_method": "api"},
            {"carried_launches": 1},
            {"started_at": float("nan")},
            {"deadline_at": 10.0},
            {"deadline_at": 611.0},
            {"per_call_seconds": 0.0},
            {"per_call_seconds": 91.0},
            {"budget": self.data["budget"] | {"max_model_calls": 3}},
            {
                "budget": self.data["budget"]
                | {"max_cost_decimal": "1", "currency": "USD"}
            },
            {
                "budget": self.data["budget"]
                | {"conservative_call_cost_decimal": "1", "currency": "USD"}
            },
        ]
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(ValueError),
            ):
                self.module.SubscriptionTerms.model_validate_json(
                    json.dumps(self.data | mutation)
                )

    def test_absolute_window_refuses_first_entry_late_and_reversed(
        self,
    ) -> None:
        for now in (9.0, 610.0, 611.0, float("nan"), float("inf")):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.module.validate_terms(
                    self.terms, self.assessment, self.selection, now
                )

    def test_selection_changes_refuse_without_private_account_effects(
        self,
    ) -> None:
        for field in (
            "candidate_digest",
            "host_identity",
            "auth_context_digest",
        ):
            changed = self.selection.model_copy(update={field: "9" * 64})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate_terms(
                    self.terms, self.assessment, changed, 11.0
                )
        for method in ("api", "unknown"):
            changed = self.selection.model_copy(update={"auth_method": method})
            with self.subTest(method=method), self.assertRaises(ValueError):
                self.module.validate_terms(
                    self.terms, self.assessment, changed, 11.0
                )
        self.assertEqual(list(self.path.iterdir()), [])

    def test_terms_digest_binds_every_mutable_candidate_setting(self) -> None:
        for field in (
            "executable_sha256",
            "schema_digest",
            "decoder_digest",
            "config_digest",
        ):
            changed = self.terms.model_copy(update={field: "9" * 64})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate_terms(
                    changed, self.assessment, self.selection, 11.0
                )

    @unittest.skipIf(sys.platform == "win32", "POSIX private authority reader")
    def test_changed_authority_and_assessment_refuse(self) -> None:
        self.authority.write_bytes(b'{"scope":"changed"}')
        with self.assertRaisesRegex(ValueError, "native_authority_changed"):
            self.module.validate_terms(
                self.terms, self.assessment, self.selection, 11.0
            )
        changed = self.assessment.model_copy(update={"id": "different"})
        with self.assertRaises(ValueError):
            self.module.validate_terms(
                self.terms, changed, self.selection, 11.0
            )

    def test_unsupported_storage_never_becomes_native_authority(self) -> None:
        with (
            mock.patch.object(sys, "platform", "win32"),
            self.assertRaisesRegex(ValueError, "capability_platform"),
        ):
            self.module.validate_terms(
                self.terms, self.assessment, self.selection, 11.0
            )
        self.assertEqual(list(self.path.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
    def test_expired_context_never_constructs_account_or_creates_lock(
        self,
    ) -> None:
        with (
            mock.patch.object(self.module.time, "time", return_value=610.0),
            mock.patch.object(
                self.module,
                "Account",
                side_effect=AssertionError("account constructed"),
            ),
            self.assertRaises(ValueError),
            self.module.subscription_account(
                self.terms, self.assessment, self.selection, initialize=True
            ),
        ):
            self.fail("expired terms opened")
        self.assertEqual(list(self.path.iterdir()), [])
        self.assertFalse(Path(self.terms.receipt_path).exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
    def test_changed_selection_context_refuses_before_account_construction(
        self,
    ) -> None:
        changes = [
            {"host_identity": "9" * 64},
            {"candidate_digest": "9" * 64},
            {"auth_context_digest": "9" * 64},
            {"auth_method": "api"},
        ]
        for mutation in changes:
            selected = self.selection.model_copy(update=mutation)
            with (
                self.subTest(mutation=mutation),
                mock.patch.object(self.module.time, "time", return_value=11.0),
                mock.patch.object(
                    self.module,
                    "Account",
                    side_effect=AssertionError("account constructed"),
                ),
                self.assertRaises(ValueError),
                self.module.subscription_account(
                    self.terms, self.assessment, selected, initialize=True
                ),
            ):
                self.fail("changed selections entered the account")
        self.assertEqual(list(self.path.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
    def test_receipt_deletion_cannot_reinitialize_unchanged_account(
        self,
    ) -> None:
        with mock.patch.object(self.module.time, "time", return_value=11.0):
            with self.module.subscription_account(
                self.terms, self.assessment, self.selection, initialize=True
            ):
                pass
            Path(self.terms.receipt_path).unlink()
            before = (self.path / "account.sqlite3").read_bytes()
            with (
                self.assertRaises(ValueError),
                self.module.subscription_account(
                    self.terms, self.assessment, self.selection
                ),
            ):
                self.fail("missing receipt was recreated")
            self.assertEqual(
                (self.path / "account.sqlite3").read_bytes(), before
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
    def test_receipt_and_terms_binding_refuse_replaced_account(self) -> None:
        with mock.patch.object(self.module.time, "time", return_value=11.0):
            with self.module.subscription_account(
                self.terms, self.assessment, self.selection, initialize=True
            ):
                pass
            receipt = Path(self.terms.receipt_path).read_bytes()
            original = self.path / "account.sqlite3"
            original.rename(self.path / "previous.sqlite3")
            with (
                self.assertRaises(ValueError),
                self.module.subscription_account(
                    self.terms,
                    self.assessment,
                    self.selection,
                    initialize=True,
                ),
            ):
                self.fail("new account reset the persistent identity")
            self.assertEqual(
                Path(self.terms.receipt_path).read_bytes(), receipt
            )

    @unittest.skipIf(sys.platform == "win32", "POSIX harness account storage")
    def test_reopening_after_deadline_keeps_original_receipt(self) -> None:
        with (
            mock.patch.object(self.module.time, "time", return_value=11.0),
            self.module.subscription_account(
                self.terms, self.assessment, self.selection, initialize=True
            ),
        ):
            pass
        receipt = Path(self.terms.receipt_path).read_bytes()
        with (
            mock.patch.object(self.module.time, "time", return_value=610.0),
            self.assertRaises(ValueError),
            self.module.subscription_account(
                self.terms, self.assessment, self.selection
            ),
        ):
            self.fail("deadline reset on reopen")
        self.assertEqual(Path(self.terms.receipt_path).read_bytes(), receipt)
