"""Check conditional launch preparation with disposable host fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import review_output, review_runner
from muninn.review_json import result_schema
from muninn.review_models import RunnerConfig
from muninn.review_rounds import round_schema
from tests.test_review_rounds import context


def digest(value: object) -> str:
    """Hash canonical JSON for independently assembled proof fields."""
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class ReviewRunnerTests(unittest.TestCase):
    """Exercise real file drift checks without running a provider."""

    def setUp(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX runner capability storage")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.proofs = self.home / "review-capabilities"
        self.proofs.mkdir(mode=0o700)
        self.cwd = self.home / "review-runner-cwd"
        self.cwd.mkdir(mode=0o700)
        self.config = self.home / "host-config"
        self.config.mkdir(mode=0o700)
        self.settings = self.config / "settings.json"
        self.settings.write_text('{"hooks":{},"plugins":[]}')
        self.executable = self.home / "fake-claude"
        self.executable.write_bytes(
            b"synthetic executable bytes; never launched"
        )
        self.executable.chmod(0o700)
        env = mock.patch.dict(
            os.environ,
            {
                "MUNINN_HOME": str(self.home),
                "HOME": str(self.home),
                "TEST_AUTH": "private fake credential",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        timer = mock.patch("time.monotonic", return_value=10.0)
        timer.start()
        self.addCleanup(timer.stop)
        self.context = context()
        runner = RunnerConfig(
            runner="claude_cli",
            executable=str(self.executable),
            version="fake-version",
            model="test-model",
            reasoning="high",
        )
        policy = self.context.policy.model_copy(
            update={
                "runner": runner,
                "result_schema_digest": digest(result_schema()),
            }
        )
        self.context = self.context.model_copy(update={"policy": policy})
        self.schema = json.dumps(
            round_schema(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.argv = [
            str(self.executable),
            "-p",
            "--json-schema",
            self.schema,
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--safe-mode",
            "--disable-slash-commands",
            "--model",
            "test-model",
            "--effort",
            "high",
        ]
        self.environment = {
            key: hashlib.sha256(os.environ[key].encode()).hexdigest()
            for key in ("HOME", "TEST_AUTH")
        }
        self.roots = (self.config, self.cwd, self.home / "absent-settings")
        self.proof = self.proof_data()
        self.capability = self.issue()

    def proof_data(self) -> dict[str, object]:
        """Assemble a simulated native receipt for disposable inputs."""
        return {
            "schema_version": 1,
            "provenance": "native_trial",
            "recipe": "claude-static-posix-v1",
            "output_contract_digest": review_output.output_contract_digest(),
            "requested": self.context.policy.runner.model_dump(),
            "schema_digest": digest(round_schema()),
            "cli_argv": self.argv,
            "executable_sha256": hashlib.sha256(
                self.executable.read_bytes()
            ).hexdigest(),
            "cli_cwd": str(self.cwd),
            "config_roots": [str(path) for path in self.roots],
            "environment_sha256": self.environment,
            "config_digest": review_runner.config_fingerprint(
                self.roots, self.environment, self.cwd
            ),
            "coverage": [
                "cwd",
                "ancestors",
                "project",
                "plugins",
                "managed",
                "auth",
            ],
            "disabled": [
                "tools",
                "mcp",
                "hooks",
                "plugins",
                "session_persistence",
            ],
            "complete": True,
        }

    def issue(self) -> review_runner.RunnerCapability:
        """Simulate controlled issuance for fake executable bytes only."""
        raw = json.dumps(self.proof).encode()
        path = self.proofs / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        return review_runner.RunnerCapability.model_validate_json(
            json.dumps(
                {
                    **self.context.policy.runner.model_dump(),
                    "status": "verified",
                    "schema_digest": self.proof["schema_digest"],
                    "config_digest": self.proof["config_digest"],
                    "proof_path": str(path),
                }
            )
        )

    def prepare(self) -> review_runner.LaunchSpec:
        """Prepare one fake round without process creation."""
        return review_runner.prepare_launch(
            self.context, self.capability, b"prompt"
        )

    def test_exact_flags_and_empty_mcp(self) -> None:
        spec = self.prepare()
        self.assertEqual(spec.cli_argv, tuple(self.argv))
        self.assertEqual(spec.cli_cwd, self.cwd)
        self.assertEqual(
            spec.cli_env,
            {"HOME": str(self.home), "TEST_AUTH": "private fake credential"},
        )
        self.assertEqual(spec.wrapper_env, {})
        self.assertEqual(
            spec.wrapper_argv[-8:],
            (
                "-m",
                "muninn.review_worker",
                "--deadline",
                "100.0",
                "--max-input-bytes",
                str(self.context.policy.max_input_bytes),
                "--max-output-bytes",
                str(self.context.policy.max_output_bytes),
            ),
        )
        self.assertTrue(
            (spec.wrapper_cwd / "muninn" / "__init__.py").is_file()
        )
        self.assertEqual(spec.prompt, b"prompt")
        self.assertEqual(spec.deadline, 100.0)

    def test_unproven_or_changed_host_config_refused(self) -> None:
        for status in ("unverified", "unsupported"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                review_runner.prepare_launch(
                    self.context,
                    self.capability.model_copy(update={"status": status}),
                    b"prompt",
                )
        self.settings.write_text('{"hooks":{"startup":"unsafe"}}')
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()

    def test_new_absent_root_and_new_directory_entry_refused(self) -> None:
        for path in (self.home / "absent-settings", self.config / "new.json"):
            path.write_text("new settings")
            with self.assertRaisesRegex(ValueError, "capability_changed"):
                self.prepare()
            path.unlink()

    def test_executable_environment_and_proof_drift_refused(self) -> None:
        self.executable.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()
        self.executable.write_bytes(
            b"synthetic executable bytes; never launched"
        )
        with (
            mock.patch.dict(
                os.environ, {"TEST_AUTH": "different private value"}
            ),
            self.assertRaisesRegex(ValueError, "capability_changed"),
        ):
            self.prepare()
        self.capability.proof_path.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()

    def test_codex_and_cursor_not_silently_supported(self) -> None:
        requested = self.context.policy.runner.model_copy(
            update={"runner": "codex_cli"}
        )
        policy = self.context.policy.model_copy(update={"runner": requested})
        context = self.context.model_copy(update={"policy": policy})
        for runner in ("codex_cli", "cursor_cli"):
            with (
                self.subTest(runner=runner),
                self.assertRaisesRegex(ValueError, "runner_unsupported"),
            ):
                review_runner.prepare_launch(
                    context,
                    self.capability.model_copy(update={"runner": runner}),
                    b"prompt",
                )

    def test_synthetic_fixture_never_authorizes_executable(self) -> None:
        fixture = json.loads(
            Path("tests/fixtures/learning/claude-capability.json").read_text()
        )
        self.proof["provenance"] = fixture["provenance"]
        self.capability = self.issue()
        with self.assertRaisesRegex(ValueError, "capability_unverified"):
            self.prepare()

    def test_flags_schema_version_and_policy_must_match(self) -> None:
        for field, value in [
            ("cli_argv", [*self.argv, "--bare"]),
            ("schema_digest", "f" * 64),
            ("complete", False),
            ("complete", 1),
            ("coverage", ["cwd"]),
            ("output_contract_digest", "f" * 64),
        ]:
            old = self.proof[field]
            self.proof[field] = value
            capability = self.issue()
            with self.subTest(field=field), self.assertRaises(ValueError):
                review_runner.prepare_launch(
                    self.context, capability, b"prompt"
                )
            self.proof[field] = old
        with self.assertRaises(ValueError):
            review_runner.prepare_launch(
                self.context,
                self.capability.model_copy(update={"version": "different"}),
                b"prompt",
            )

    def test_unsafe_permissions_symlinks_and_storage_refused(self) -> None:
        self.capability.proof_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "capability_storage"):
            self.prepare()
        self.capability.proof_path.chmod(0o600)
        link = self.config / "linked"
        link.symlink_to(self.settings)
        with self.assertRaises(ValueError):
            self.prepare()
        link.unlink()
        outside = self.home / self.capability.proof_path.name
        outside.write_bytes(self.capability.proof_path.read_bytes())
        outside.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "capability_storage"):
            review_runner.prepare_launch(
                self.context,
                self.capability.model_copy(update={"proof_path": outside}),
                b"prompt",
            )

    def test_input_round_and_deadline_limits(self) -> None:
        for changed, prompt in [
            (self.context, b"x" * (self.context.policy.max_input_bytes + 1)),
            (self.context.model_copy(update={"round_index": 3}), b"x"),
            (self.context.model_copy(update={"deadline": 10.0}), b"x"),
        ]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                review_runner.prepare_launch(changed, self.capability, prompt)

    def test_structural_file_limit_refuses(self) -> None:
        self.settings.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "capability_limit"):
            self.prepare()

    def test_invalid_proof_and_metadata_do_not_expose_values(self) -> None:
        self.proof["complete"] = "private invalid marker"
        self.capability = self.issue()
        with self.assertRaisesRegex(ValueError, "^capability_invalid$"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "^invalid_reported_metadata$"):
            review_runner.reported_metadata(
                {"password": "private value"}, None
            )

    def test_removed_root_and_incomplete_attestations_refused(self) -> None:
        self.settings.unlink()
        with self.assertRaisesRegex(ValueError, "capability_changed"):
            self.prepare()
        for field, value in [
            ("disabled", ["tools"]),
            ("environment_sha256", {}),
            ("config_roots", []),
        ]:
            self.proof[field] = value
            self.capability = self.issue()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare()

    def test_inventory_entry_limit_refuses(self) -> None:
        for number in range(4097):
            (self.config / str(number)).touch()
        with self.assertRaisesRegex(ValueError, "capability_limit"):
            self.prepare()

    def test_access_time_changes_do_not_invalidate_content_proof(self) -> None:
        for path in (
            self.settings,
            self.executable,
            self.capability.proof_path,
        ):
            os.utime(path, ns=(1, path.stat().st_mtime_ns))
        self.assertEqual(self.prepare().prompt, b"prompt")

    def test_aggregate_config_and_executable_caps_refuse(self) -> None:
        for number in range(17):
            (self.config / str(number)).write_bytes(b"x" * (1024 * 1024))
        with self.assertRaisesRegex(ValueError, "capability_limit"):
            self.prepare()
        with self.executable.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "capability_limit"):
            self.prepare()

    def test_proof_size_cap_refuses_before_parsing(self) -> None:
        raw = b"x" * (1024 * 1024 + 1)
        path = self.proofs / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "capability_limit"):
            review_runner.prepare_launch(
                self.context,
                self.capability.model_copy(update={"proof_path": path}),
                b"prompt",
            )

    def test_json_conversion_and_recursion_failures_are_finite(self) -> None:
        for raw in (b"1" * 5000, b"[" * 5000 + b"0" + b"]" * 5000):
            path = self.proofs / (hashlib.sha256(raw).hexdigest() + ".json")
            path.write_bytes(raw)
            path.chmod(0o600)
            capability = self.capability.model_copy(
                update={"proof_path": path}
            )
            with (
                self.subTest(size=len(raw)),
                self.assertRaisesRegex(ValueError, "^capability_invalid$"),
            ):
                review_runner.prepare_launch(
                    self.context, capability, b"prompt"
                )

    def test_missing_decoder_contract_binding_refuses(self) -> None:
        del self.proof["output_contract_digest"]
        self.capability = self.issue()
        with self.assertRaisesRegex(ValueError, "^capability_invalid$"):
            self.prepare()
