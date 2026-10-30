"""Behavioral checks for a complete non-writing host-only preview."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from unittest import mock

from install import host_renewal_bindings, preflight
from install.context import StepFailedError
from install.host_renewal_preview import preview, summary
from install.provider_paths import cursor_commands
from install.transforms import edit_cursor_settings
from muninn import review_codex_launch
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from tests.host_renewal_record_support import artifact_inputs
from tests.host_renewal_support import HostFixture, states
from tests.installer_support import git
from tools import prepare_codex_policy
from tools.codex_policy_artifacts import plan_artifacts


class HostRenewalPreviewTests(HostFixture):
    """Bind exact policy, artifact and complete durable-state observations."""

    def test_preview_is_deterministic_and_nonwriting(self) -> None:
        before = states(self.home)
        first = preview(self.ctx, self.repo, self.sha)
        second = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(states(self.home), before)
        self.assertNotEqual(first.old["digest"], first.candidate["digest"])
        other = dataclasses.replace(
            self.ctx, ts="other-operation", now=lambda: 900.0
        )
        self.assertEqual(
            preview(other, self.repo, self.sha).digest, first.digest
        )

    def test_only_host_delta_is_accepted(self) -> None:
        preview(self.ctx, self.repo, self.sha)
        original = self.candidate.model_dump(mode="json")
        paths: list[tuple[tuple[str | int, ...], object]] = []

        def leaves(value: object, path: tuple[str | int, ...]) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    leaves(item, (*path, key))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    leaves(item, (*path, index))
            else:
                paths.append((path, value))

        leaves(original, ())
        for path, value in paths:
            data = json.loads(json.dumps(original))
            target = data
            for key in path[:-1]:
                target = target[key]
            replacement = (
                (not value)
                if isinstance(value, bool)
                else (
                    value + 1
                    if isinstance(value, (int, float))
                    else str(value) + "changed"
                )
            )
            if path == ("policy_source",):
                replacement = "repository_default"
            elif path[-1] in (
                "host_identity",
                "prompt_digest",
                "result_schema_digest",
            ):
                replacement = "f" * 64
            target[path[-1]] = replacement
            assert self.ctx.review_policy is not None
            self.ctx.review_policy.write_text(json.dumps(data))
            before = states(self.home)
            with self.subTest(field=path), self.assertRaises(StepFailedError):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)
        self.write_candidate(self.old)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)

    def test_reused_digest_refuses_every_request_state(self) -> None:
        for state in (
            "pending",
            "running",
            "waiting",
            "failed",
            "completed",
            "cancelled",
        ):
            with self.subTest(state=state):
                self.sql("DELETE FROM review_request")
                self.request(state, candidate=True)
                before = states(self.home)
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_digest_reused"
                ):
                    preview(self.ctx, self.repo, self.sha)
                self.assertEqual(states(self.home), before)

    def test_artifact_recipe_matches_independent_bytes(self) -> None:
        binding, prompt = artifact_inputs(self)
        plan = plan_artifacts(self.candidate, prompt, binding, "c" * 64)
        proof_raw = canonical_json(binding).encode()
        proof_digest = hashlib.sha256(proof_raw).hexdigest()
        candidate_digest = hashlib.sha256(
            canonical_json(self.candidate).encode()
        ).hexdigest()
        expected = {
            "review-codex-schema.json": (
                b'{"additionalProperties":false,"properties":{'
                b'"payload_json":{"type":"string"}},"required":'
                b'["payload_json"],"type":"object"}'
            ),
            f"review-prompts/{self.old.prompt_digest}.json": prompt,
            f"review-capabilities/{proof_digest}.json": proof_raw,
            (
                "review-capabilities/selected/" f"{candidate_digest}.json"
            ): canonical_json_value(
                {
                    **self.candidate.runner.model_dump(mode="json"),
                    "status": "unverified",
                    "proof_path": str(
                        self.ctx.data
                        / "review-capabilities"
                        / f"{proof_digest}.json"
                    ),
                    "schema_digest": hashlib.sha256(
                        canonical_json_value(round_schema()).encode()
                    ).hexdigest(),
                    "config_digest": "c" * 64,
                }
            ).encode(),
        }
        self.assertEqual(
            {item.relative_path.as_posix(): item.raw for item in plan.files},
            expected,
        )
        with mock.patch(
            "pathlib.Path.read_bytes",
            side_effect=AssertionError("pure recipe read"),
        ):
            self.assertEqual(
                plan_artifacts(self.candidate, prompt, binding, "c" * 64), plan
            )

    def test_deadline_and_unsafe_store_refuse_without_writes(self) -> None:
        before = states(self.home)
        with (
            mock.patch(
                "install.host_renewal_scan.time.monotonic",
                side_effect=[0.0, 31.0],
            ),
            self.assertRaisesRegex(StepFailedError, "host_renewal_limit"),
        ):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)
        (self.ctx.data / "muninn.sqlite-wal").write_bytes(b"unsafe")
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_summary_does_not_disclose_private_bodies(self) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        public = json.dumps(summary(plan))
        for private in (
            canonical_json(self.old),
            self.old.budget.account_id,
            "sk-fake-",
            "host_identity",
            "fixture-owner",
        ):
            self.assertNotIn(private, public)

    def test_completed_and_unresolved_calls_are_never_contained(self) -> None:
        request = self.request()
        attempt = self.attempt(request)
        for state in ("completed", "reserved", "launched", "unknown"):
            self.sql("DELETE FROM review_call")
            self.sql(
                "INSERT INTO review_call(attempt_id,round_index,"
                "budget_account_id,"
                "policy_digest,state,reserved_at,requested_json,"
                "observed_json) "
                "VALUES(?,1,'account',?,?,1.0,'{}','{}')",
                (attempt, digest(self.old), state),
            )
            before = states(self.home)
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(
                    StepFailedError, "host_renewal_containment_required"
                ),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)

    def test_artifact_and_existing_lock_changes_refuse_or_change_digest(
        self,
    ) -> None:
        initial = preview(self.ctx, self.repo, self.sha)
        lock = self.ctx.lib / ".install-record.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        self.assertIsNone(initial.operation_lock)
        checked = preview(self.ctx, self.repo, self.sha)
        self.assertIsNotNone(checked.operation_lock)
        self.assertNotEqual(initial.digest, checked.digest)
        for path in (
            self.ctx.data / "review-codex-schema.json",
            self.ctx.data / "writer.lock",
        ):
            before_raw = path.read_bytes()
            (
                path.write_bytes(b"changed")
                if path.name.endswith("json")
                else path.unlink()
            )
            before = states(self.home)
            with (
                self.subTest(path=path.name),
                self.assertRaises(StepFailedError),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)
            path.write_bytes(before_raw)
            path.chmod(0o600)

    def test_current_format_two_retains_both_default_policy_revisions(
        self,
    ) -> None:
        for revision in (1, 2):
            with (
                self.subTest(policy_revision=revision),
                mock.patch(
                    "muninn.review_codex_policy.host_identity",
                    return_value=self.old_host,
                ),
            ):
                path = prepare_codex_policy.prepare(
                    self.ctx.data,
                    self.executable,
                    self.ctx.codex_home,
                    "fixture-owner",
                    1.0,
                    revision=revision,
                    default_profile=(
                        self.repo / "tools/default_codex_policy.json"
                    ).read_bytes(),
                )
            old = ReviewPolicy.model_validate_json(path.read_bytes())
            self.sql(
                "UPDATE review_control SET policy_json=?,policy_digest=?",
                (canonical_json(old), digest(old)),
            )
            data = old.model_dump(mode="json")
            data["experimental_codex"]["host_identity"] = self.current_host
            candidate = ReviewPolicy.model_validate_json(json.dumps(data))
            self.write_candidate(candidate)
            before = states(self.home)
            plan = preview(self.ctx, self.repo, self.sha)
            self.assertEqual(
                plan.candidate["canonical"], canonical_json(candidate)
            )
            self.assertEqual(candidate.policy_source, "repository_default")
            self.assertEqual(candidate.budget, old.budget)
            self.assertEqual(states(self.home), before)

    def test_preview_cannot_provision_or_launch(self) -> None:
        with (
            mock.patch(
                "tools.prepare_codex_policy.prepare",
                side_effect=AssertionError("publication"),
            ),
            mock.patch(
                "install.record.save",
                side_effect=AssertionError("record publication"),
            ),
            mock.patch(
                "muninn.store.writer_lock",
                side_effect=AssertionError("writer provisioning"),
            ),
            mock.patch(
                "muninn.review_pause.maintenance_owner",
                side_effect=AssertionError("owner provisioning"),
            ),
            mock.patch(
                "muninn.review_codex_launch.prepare",
                side_effect=AssertionError("provider launch"),
            ),
        ):
            plan = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(plan.kind, "codex_host_renewal_v1")
        self.assertFalse(self.ctx.rdir.exists())

    def test_pure_recipe_refuses_changed_bindings_and_prompt(self) -> None:
        binding, prompt = artifact_inputs(self)
        data = binding.model_dump(mode="json")
        original = plan_artifacts(self.candidate, prompt, binding, "c" * 64)
        for key, value in (
            ("host_identity", "f" * 64),
            ("output_contract_digest", "f" * 64),
            ("schema_path", str(self.ctx.data / "wrong.json")),
            ("cli_cwd", str(self.ctx.data / "wrong-cwd")),
            ("auth_home", "relative"),
        ):
            changed = (
                review_codex_launch.ExperimentalBinding.model_validate_json(
                    json.dumps(data | {key: value})
                )
            )
            with self.subTest(key=key), self.assertRaises(ValueError):
                plan_artifacts(self.candidate, prompt, changed, "c" * 64)
        with self.assertRaises(ValueError):
            plan_artifacts(self.candidate, prompt + b" ", binding, "c" * 64)
        changed_config = plan_artifacts(
            self.candidate, prompt, binding, "d" * 64
        )
        self.assertNotEqual(original.digest, changed_config.digest)
        self.assertNotEqual(
            {item.relative_path: item.raw for item in original.files},
            {item.relative_path: item.raw for item in changed_config.files},
        )

    def test_last_signature_drift_and_target_contract_change_refuse(
        self,
    ) -> None:
        journal = self.ctx.data / "tombstones.jsonl"
        journal.write_bytes(b"")
        journal.chmod(0o600)
        calls = [0]

        def signature(executable: object) -> tuple[object, str]:
            calls[0] += 1
            if calls[0] == 4:
                journal.write_bytes(
                    b'{"provider":"codex","level":"thread","thread_id":"changed","created_at":1.0}\n'
                )
            return self.executable, "a" * 64

        with (
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                side_effect=signature,
            ),
            self.assertRaisesRegex(
                StepFailedError, "host_renewal_state_changed"
            ),
        ):
            preview(self.ctx, self.repo, self.sha)
        contract = self.repo / "muninn/review_codex_output.py"
        contract.write_bytes(
            contract.read_bytes() + b"\n# Different output contract.\n"
        )
        git(self.repo, "add", "muninn/review_codex_output.py")
        git(self.repo, "commit", "-qm", "different synthetic contract")
        sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_artifacts_changed"
        ):
            preview(self.ctx, self.repo, sha)
        self.assertEqual(states(self.home), before)

    def test_ambiguous_policy_and_unsafe_auth_directory_refuse(self) -> None:
        assert self.ctx.review_policy is not None
        raw = self.ctx.review_policy.read_text()
        self.ctx.review_policy.write_text(
            raw[:-1]
            + ',"policy_id":'
            + json.dumps(self.candidate.policy_id)
            + "}"
        )
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)
        self.write_candidate(self.candidate)
        auth = self.ctx.codex_home
        auth.rename(self.home / "auth-saved")
        auth.symlink_to(self.home / "auth-saved", target_is_directory=True)
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_absent_cursor_creation_is_bound_to_the_plan(self) -> None:
        self.assertFalse(self.ctx.cursor_settings.exists())
        before = states(self.home)
        disabled = preview(self.ctx, self.repo, self.sha)
        self.ctx.cursor_hooks = True
        enabled = preview(self.ctx, self.repo, self.sha)
        self.assertNotEqual(disabled.digest, enabled.digest)
        files, _ = preflight.check_repo(self.ctx, self.repo, self.sha)
        expected = edit_cursor_settings(
            preflight.EMPTY_CURSOR_CONFIG,
            cursor_commands(self.ctx),
            json.loads(files[preflight.CURSOR_HOOKS_FILE])["hooks"],
        )
        configuration = host_renewal_bindings.configuration(self.ctx, files)
        self.assertTrue(configuration["cursor_hooks"])
        self.assertEqual(
            configuration["cursor"],
            {
                "path": str(self.ctx.cursor_settings),
                "before": None,
                "expected": [hashlib.sha256(expected).hexdigest()],
            },
        )
        self.assertEqual(states(self.home), before)
