"""Check stable selection authority and bounded per-call identity history."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from pydantic import ValidationError

from muninn import review_models, review_runner, review_runtime_authority
from muninn.review_json import canonical_json
from muninn.review_runtime_models import (
    ManagedCallRecord,
    require_legacy_runner,
)
from tests.managed_codex_support import identity, selection
from tests.test_review_codex_production import experimental_policy


class RuntimeModelTests(TestCase):
    """Keep a managed update separate from policy authority."""

    def test_managed_selection_has_no_runtime_authority(self) -> None:
        factory = getattr(review_models, "ManagedCodexSelection", None)
        self.assertIsNotNone(factory, "explicit managed selection is missing")
        assert factory is not None
        selected = factory(
            selection="codex_managed_interface_v1",
            runner="codex_cli",
            manager_root=Path("/Users/owner/.codex/packages/standalone"),
            selector="current",
            interface="codex-json-review-v1",
            model="gpt-6.1-sol",
            reasoning="low",
        )
        policy = experimental_policy(Path("/private/muninn")).model_copy(
            update={"runner": selected}
        )
        raw = canonical_json(policy)
        self.assertNotIn('"version"', json.dumps(json.loads(raw)["runner"]))
        self.assertEqual(
            canonical_json(
                review_models.ReviewPolicy.model_validate_json(raw)
            ),
            raw,
        )
        for field in ("version", "executable", "executable_sha256"):
            with self.assertRaises(ValidationError):
                factory(**selected.model_dump(), **{field: "unapproved"})

    def test_legacy_runner_json_stays_exact(self) -> None:
        runner = review_models.RunnerConfig(
            runner="codex_cli",
            executable="/exact/codex",
            version="0.163.0",
            model="gpt-6.1-sol",
            reasoning="low",
        )
        self.assertEqual(
            canonical_json(runner),
            (
                '{"executable":"/exact/codex","model":"gpt-6.1-sol",'
                '"reasoning":"low","runner":"codex_cli","version":"0.163.0"}'
            ),
        )

    def test_legacy_recipe_cannot_reinterpret_managed_selection(self) -> None:
        selected = selection(Path("/fixture/manager"))
        with self.assertRaisesRegex(
            ValueError, "runtime_interface_unsupported"
        ):
            require_legacy_runner(selected)

    def test_call_record_cannot_invent_a_launched_identity(self) -> None:
        selected = selection(Path("/fixture/manager"))
        observed = identity(selected.manager_root)
        for changes in (
            {"launched_identity": "a" * 64},
            {"candidates": (observed, observed)},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ValidationError),
            ):
                ManagedCallRecord.model_validate(
                    {
                        "record": "codex-managed-call-v1",
                        "selection": selected,
                        "candidates": (observed,),
                    }
                    | changes
                )

    def test_legacy_private_readers_remain_the_same_public_objects(
        self,
    ) -> None:
        self.assertIs(
            review_runner.read_private_file,
            review_runtime_authority.read_private_file,
        )
        self.assertIs(
            review_runner.check_private_path,
            review_runtime_authority.check_private_path,
        )
        self.assertIs(
            review_runner._canonical, review_runtime_authority.canonical_path
        )
        self.assertIs(
            review_runner._identity, review_runtime_authority.file_identity
        )

    def test_shared_authority_imports_without_order_dependent_cycles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for first in (
                "muninn.review_claims",
                "muninn.review_runner",
                "install.learning_policy_recovery",
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import importlib; importlib.import_modul"
                        "e(__import__('sys').argv[1]); import ins"
                        "tall.entry; import muninn.review_runtime"
                        "_authority",
                        first,
                    ],
                    cwd=Path(__file__).resolve().parent.parent,
                    env={
                        "HOME": temporary,
                        "MUNINN_HOME": temporary,
                        "PATH": os.defpath,
                    },
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
