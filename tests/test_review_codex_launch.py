"""Check explicit experimental preparation with only disposable files."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest import TestCase, mock, skipIf

from muninn import review_codex_launch as launch
from muninn import review_runner
from muninn.knowledge_models import Scope
from muninn.review_codex_output import output_contract_digest
from muninn.review_json import canonical_json_value, result_schema
from muninn.review_rounds import round_schema
from tests.ingest_support import IngestCase
from tests.test_review_codex_production import experimental_policy
from tests.test_review_rounds import context
from tools import prepare_codex_policy


@skipIf(sys.platform == "win32", "macOS private-path contract")
class CodexProductionLaunchTests(IngestCase):
    """Exercise actual preparation and drift checks without launching a CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = experimental_policy(self.home)
        executable = self.home / "fake-codex"
        executable.write_text("synthetic bytes; not an executable program")
        executable.chmod(0o700)
        self.executable_hash = hashlib.sha256(
            executable.read_bytes()
        ).hexdigest()
        self.policy = self.policy.model_copy(
            update={
                "runner": self.policy.runner.model_copy(
                    update={"executable": str(executable)}
                ),
                "result_schema_digest": hashlib.sha256(
                    canonical_json_value(result_schema()).encode()
                ).hexdigest(),
            }
        )
        self.context = context().model_copy(
            update={
                "policy": self.policy,
                "scope": Scope(project_id="project", task=None),
            }
        )
        (self.home / "recall.off").touch(mode=0o600)
        cwd = self.home / "review-runner-cwd"
        cwd.mkdir(mode=0o700)
        auth = self.tmp / "fake-auth"
        auth.mkdir(mode=0o700)
        schema = self.home / "review-codex-schema.json"
        schema.write_bytes(launch.wire_schema())
        schema.chmod(0o600)
        self.binding = launch.ExperimentalBinding(
            requested=self.policy.runner,
            host_identity="1" * 64,
            executable_sha256=self.executable_hash,
            output_contract_digest=output_contract_digest(),
            cli_cwd=cwd,
            auth_home=auth,
            schema_path=schema,
        )
        raw = canonical_json_value(
            self.binding.model_dump(mode="json")
        ).encode()
        proofs = self.home / "review-capabilities"
        proofs.mkdir(mode=0o700)
        proof = proofs / (hashlib.sha256(raw).hexdigest() + ".json")
        proof.write_bytes(raw)
        proof.chmod(0o600)
        self.capability = review_runner.RunnerCapability(
            **self.policy.runner.model_dump(),
            status="unverified",
            schema_digest=hashlib.sha256(
                canonical_json_value(round_schema()).encode()
            ).hexdigest(),
            config_digest=launch.fingerprint(self.binding),
            proof_path=proof,
        )
        for patch in (
            mock.patch.dict(os.environ, {"MUNINN_HOME": str(self.home)}),
            mock.patch.object(launch, "host_identity", return_value="1" * 64),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("time.time", return_value=11.0),
            mock.patch("time.monotonic", return_value=10.0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.prompt = canonical_json_value(
            {"instructions": launch.JSON_ONLY_INSTRUCTION, "data": {}}
        ).encode()

    def test_explicit_selection_uses_fixed_recipe_and_envelope(self) -> None:
        spec = review_runner.prepare_launch(
            self.context, self.capability, self.prompt
        )
        self.assertIn("--ephemeral", spec.cli_argv)
        self.assertIn("--ignore-user-config", spec.cli_argv)
        self.assertIn("--ignore-rules", spec.cli_argv)
        self.assertEqual(
            spec.cli_env["CODEX_HOME"], str(self.binding.auth_home)
        )
        self.assertEqual(
            json.loads(spec.prompt)["payload_schema"], round_schema()
        )
        self.assertTrue(
            json.loads(spec.prompt)["instructions"].endswith(
                launch.WIRE_INSTRUCTION
            )
        )
        self.assertLessEqual(spec.deadline, self.context.deadline)

    def test_default_and_forged_verified_do_not_admit_codex(self) -> None:
        policy = self.policy.model_copy(update={"experimental_codex": None})
        with self.assertRaisesRegex(ValueError, "capability_invalid"):
            review_runner.prepare_launch(
                self.context.model_copy(update={"policy": policy}),
                self.capability,
                self.prompt,
            )
        with self.assertRaisesRegex(ValueError, "codex_experimental_boundary"):
            review_runner.prepare_launch(
                self.context,
                self.capability.model_copy(update={"status": "verified"}),
                self.prompt,
            )

    def test_preparation_writes_complete_artifacts_without_activation(
        self,
    ) -> None:
        path = prepare_codex_policy.prepare(
            self.home,
            Path(self.policy.runner.executable),
            self.binding.auth_home,
            "synthetic-fixture-owner",
            10.0,
        )
        revised = prepare_codex_policy.prepare(
            self.home,
            Path(self.policy.runner.executable),
            self.binding.auth_home,
            "synthetic-fixture-owner",
            11.0,
            revision=2,
        )
        raw = path.read_bytes()
        selected = (
            self.home
            / "review-capabilities"
            / "selected"
            / (hashlib.sha256(raw).hexdigest() + ".json")
        )
        capability = review_runner.RunnerCapability.model_validate_json(
            selected.read_bytes()
        )
        self.assertEqual(capability.status, "unverified")
        self.assertTrue(capability.proof_path.is_file())
        self.assertEqual(
            json.loads(raw)["budget"]["kind"], "ongoing_production"
        )
        self.assertEqual(json.loads(raw)["runner"]["version"], "any")
        self.assertEqual(
            json.loads(capability.proof_path.read_bytes())[
                "executable_sha256"
            ],
            self.executable_hash,
        )
        self.assertNotEqual(path, revised)
        self.assertEqual(
            json.loads(revised.read_bytes())["max_input_bytes"], 1_048_576
        )

    def test_other_project_allowed_and_codex_upgrade_keeps_review_controls(
        self,
    ) -> None:
        other = self.context.model_copy(
            update={"scope": Scope(project_id="other", task=None)}
        )
        review_runner.prepare_launch(other, self.capability, self.prompt)
        (self.home / "recall.off").unlink()
        with self.assertRaisesRegex(ValueError, "codex_experimental_boundary"):
            review_runner.prepare_launch(
                self.context, self.capability, self.prompt
            )
        (self.home / "recall.off").touch()
        Path(self.policy.runner.executable).write_text("new executable bytes")
        spec = review_runner.prepare_launch(
            self.context, self.capability, self.prompt
        )
        self.assertEqual(spec.cli_argv[0], self.policy.runner.executable)
        self.assertIn("read-only", spec.cli_argv)

    def test_existing_policy_survives_codex_upgrade(self) -> None:
        """Keep old installed policy artifacts usable after the CLI changes."""
        runner = self.policy.runner.model_copy(update={"version": "0.163.0"})
        policy = self.policy.model_copy(update={"runner": runner})
        binding = self.binding.model_copy(update={"requested": runner})
        raw = canonical_json_value(binding.model_dump(mode="json")).encode()
        proof = (
            self.home
            / "review-capabilities"
            / (hashlib.sha256(raw).hexdigest() + ".json")
        )
        proof.write_bytes(raw)
        proof.chmod(0o600)
        capability = self.capability.model_copy(
            update={
                "version": "0.163.0",
                "config_digest": launch.fingerprint(binding),
                "proof_path": proof,
            }
        )
        context = self.context.model_copy(update={"policy": policy})
        review_runner.prepare_launch(context, capability, self.prompt)
        Path(runner.executable).write_text("upgraded Codex executable")
        spec = review_runner.prepare_launch(context, capability, self.prompt)
        self.assertEqual(spec.cli_argv[0], runner.executable)


class CodexWindowsBoundaryTests(TestCase):
    """Keep the explicit platform rejection covered on every host."""

    def test_windows_private_path_refused(self) -> None:
        with (
            mock.patch.object(review_runner.sys, "platform", "win32"),
            self.assertRaisesRegex(ValueError, "capability_platform"),
        ):
            review_runner.check_private_path(Path("/unused"), directory=True)
