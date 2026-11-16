"""Load approved historical receipts without changing their signed fields."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from collections.abc import Mapping
from typing import Any

from install import codex_interface_preview, policy_records, record
from install.context import StepFailedError
from tests import host_renewal_record_support as renewal
from tests import test_codex_interface_preview as interface

VOLUME = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"


def signed_digest(value: dict[str, Any]) -> str:
    """Hash independently encoded approved JSON without model defaults."""
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


@unittest.skipIf(sys.platform == "win32", "requires POSIX")
class InterfaceWitnessReceiptTests(unittest.TestCase):
    """Keep strict interface approval checks across historical wire shapes."""

    def setUp(self) -> None:
        self.fixture = interface.InterfacePreviewTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.ctx.rdir.mkdir(mode=0o700)

    def envelope(self, volume: Mapping[str, object]) -> dict[str, Any]:
        """Build an approved receipt from explicit original JSON fields."""
        fixture = self.fixture
        plan = codex_interface_preview.preview(fixture.ctx).model_dump(
            mode="json"
        )
        witness = plan["containment"]["witness"]
        witness.pop("volume_uuid", None)
        witness.update(volume)
        return {
            "kind": "codex-managed-interface-receipt-v1",
            "phase": "approved_intent",
            "operation": fixture.ctx.ts,
            "home": str(fixture.home),
            "repo": str(fixture.repo),
            "sha": fixture.sha,
            "plan": plan,
            "approval": {
                "plan_digest": signed_digest(plan),
                "approved_at": 10.0,
                "authority": "installer_prompt_yes",
            },
            "operation_identity": [1, 2],
            "predecessor": None,
            "successor": None,
            "contained": None,
            "artifacts_digest": "a" * 64,
            "minimum_runtime": plan["minimum_runtime"],
        }

    def test_public_receipt_loading_and_save_keep_original_plan_digest(
        self,
    ) -> None:
        for volume in ({}, {"volume_uuid": None}, {"volume_uuid": VOLUME}):
            raw = self.envelope(volume)
            expected = raw["approval"]["plan_digest"]
            with self.subTest(volume=volume):
                receipt = policy_records.codex_interface(raw)
                self.assertEqual(receipt.plan.digest, expected)
                self.assertEqual(receipt.plan.model_copy().digest, expected)
                self.assertEqual(
                    receipt.plan.model_copy(deep=True).digest, expected
                )
                projected = receipt.model_dump(mode="json")
                self.assertEqual(projected["plan"], raw["plan"])
                record.save(self.fixture.ctx, {"codex_interface": projected})
                saved = record.load_record(
                    self.fixture.ctx.rdir / "rollback-record.json"
                )
                restored = policy_records.codex_interface(
                    saved["codex_interface"]
                )
                self.assertEqual(restored.plan.digest, expected)

    def test_changed_witness_never_reuses_original_approval(self) -> None:
        raw = self.envelope({})
        raw["plan"]["containment"]["witness"]["volume_uuid"] = None
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_record_invalid"
        ):
            policy_records.codex_interface(raw)
        raw = self.envelope({"volume_uuid": VOLUME})
        raw["plan"]["containment"]["witness"][
            "volume_uuid"
        ] = "87654321-1234-4234-8234-123456789abc"
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_record_invalid"
        ):
            policy_records.codex_interface(raw)

    def test_explicit_null_copy_changes_plan_digest(self) -> None:
        receipt = policy_records.codex_interface(self.envelope({}))
        original = receipt.plan
        changed_witness = original.containment.witness.model_copy(
            update={"volume_uuid": None}
        )
        changed = original.model_copy(
            update={
                "containment": original.containment.model_copy(
                    update={"witness": changed_witness}
                )
            }
        )
        self.assertNotEqual(changed.digest, original.digest)


@unittest.skipIf(sys.platform == "win32", "requires POSIX")
class HostWitnessReceiptTests(unittest.TestCase):
    """Keep the second digest-bearing witness consumer compatible."""

    def setUp(self) -> None:
        self.fixture = renewal.ReceiptFixture()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.ctx.rdir.mkdir(mode=0o700)

    def test_host_renewal_receipt_keeps_each_original_witness_shape(
        self,
    ) -> None:
        for volume in ({}, {"volume_uuid": None}, {"volume_uuid": VOLUME}):
            raw = self.fixture.initial()
            plan = raw["plan"]
            witness = plan["containment"]["witness"]
            witness.pop("volume_uuid", None)
            witness.update(volume)
            plan["digest"] = signed_digest(
                {key: value for key, value in plan.items() if key != "digest"}
            )
            raw["approval"]["plan_digest"] = plan["digest"]
            with self.subTest(volume=volume):
                receipt = policy_records.host_renewal(raw)
                self.assertEqual(receipt.plan.digest, plan["digest"])
                projected = receipt.model_dump(mode="json")
                self.assertEqual(
                    projected["plan"]["containment"]["witness"], witness
                )
                record.save(self.fixture.ctx, {"host_renewal": projected})
                saved = record.load_record(
                    self.fixture.ctx.rdir / "rollback-record.json"
                )
                self.assertEqual(
                    policy_records.host_renewal(
                        saved["host_renewal"]
                    ).plan.digest,
                    plan["digest"],
                )
