"""Check stable managed proof and truthful resolved launch arguments."""

from __future__ import annotations

import hashlib
import time
from unittest import mock

from muninn import review_managed_launch as launch
from muninn import review_runner
from muninn.review_codex_launch import wire_schema
from muninn.review_codex_output import output_contract_digest
from muninn.review_json import canonical_json
from muninn.review_runtime_models import (
    EnrollmentApproval,
    ManagedCapability,
    ManagedInterfaceBinding,
)
from tests.managed_codex_support import (
    host_enrollment,
    identity,
    managed_policy,
    physical_host,
    selection,
)
from tests.test_review_codex_launch import CodexProductionLaunchTests


class ManagedLaunchTests(CodexProductionLaunchTests):
    """Use only temporary metadata and mocked external runtime observation."""

    def managed(self) -> tuple[ManagedCapability, ManagedInterfaceBinding]:
        """Publish an approved fixture without touching real HOME."""
        selected = selection(self.tmp / "manager")
        old = self.policy
        self.policy = managed_policy(old, selected.manager_root)
        self.context = self.context.model_copy(
            update={
                "policy": self.policy,
                "deadline": time.monotonic() + 120,
            }
        )
        approval = EnrollmentApproval(
            kind="codex-managed-enrollment-v1",
            old_policy_digest="a" * 64,
            new_policy_digest=hashlib.sha256(
                canonical_json(self.policy).encode()
            ).hexdigest(),
            bridge_digest=None,
            host_identity=physical_host(),
            host_enrollment=host_enrollment(old, self.policy),
            legacy_artifacts_digest="c" * 64,
            auth_home=self.binding.auth_home,
            operation_id="fixture-operation",
            plan_digest="b" * 64,
            approved_at=10.0,
            approval="owner_yes",
        )
        directory = self.home / "review-capabilities" / "managed-approvals"
        directory.mkdir(mode=0o700)
        raw = canonical_json(approval).encode()
        approved = hashlib.sha256(raw).hexdigest()
        path = directory / (approved + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        binding = ManagedInterfaceBinding(
            kind="codex-managed-binding-v1",
            requested=selected,
            host_identity=physical_host(),
            auth_home=self.binding.auth_home,
            cli_cwd=self.binding.cli_cwd,
            schema_path=self.binding.schema_path,
            output_contract_digest=output_contract_digest(),
            enrollment_approval=approved,
            initial_bridge=None,
        )
        raw = canonical_json(binding).encode()
        path = (
            self.home
            / "review-capabilities"
            / (hashlib.sha256(raw).hexdigest() + ".json")
        )
        path.write_bytes(raw)
        path.chmod(0o600)
        return (
            ManagedCapability(
                kind="codex-managed-capability-v1",
                requested=selected,
                proof_path=path,
                schema_digest=self.capability.schema_digest,
                config_digest=launch.fingerprint(binding),
            ),
            binding,
        )

    def test_stable_policy_launches_current_checked_identity(self) -> None:
        capability, binding = self.managed()
        before = canonical_json(self.policy)
        observed = identity(binding.requested.manager_root)
        with (
            mock.patch(
                "muninn.review_managed_files.observe", return_value=observed
            ),
            mock.patch(
                "muninn.review_managed_probe.negotiate", return_value=observed
            ),
        ):
            spec = review_runner.prepare_launch(
                self.context, capability, self.prompt
            )
        self.assertEqual(spec.cli_argv[0], observed.requested.executable)
        self.assertEqual(spec.execution, observed)
        self.assertEqual(canonical_json(self.policy), before)
        self.assertEqual(spec.cli_env["CODEX_HOME"], str(binding.auth_home))
        self.assertEqual(wire_schema(), binding.schema_path.read_bytes())

    def test_missing_enrollment_cannot_reconstruct_consent(self) -> None:
        capability, binding = self.managed()
        path = (
            self.home
            / "review-capabilities"
            / "managed-approvals"
            / (binding.enrollment_approval + ".json")
        )
        path.unlink()
        with self.assertRaisesRegex(ValueError, "capability_unavailable"):
            review_runner.prepare_launch(self.context, capability, self.prompt)

    def test_renamed_host_keeps_same_physical_grant_and_launch(self) -> None:
        capability, binding = self.managed()
        before = canonical_json(self.policy)
        observed = identity(binding.requested.manager_root)
        with (
            mock.patch("platform.node", return_value="renamed-synthetic-host"),
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                side_effect=AssertionError("managed launch read legacy name"),
            ),
            mock.patch(
                "muninn.review_managed_files.observe", return_value=observed
            ),
            mock.patch(
                "muninn.review_managed_probe.negotiate", return_value=observed
            ),
        ):
            spec = review_runner.prepare_launch(
                self.context, capability, self.prompt
            )
        self.assertEqual(spec.execution, observed)
        self.assertEqual(canonical_json(self.policy), before)

    def test_foreign_or_unavailable_physical_identity_precedes_package_probe(
        self,
    ) -> None:
        capability, _ = self.managed()
        for value in (
            physical_host("7" * 64),
            ValueError("codex_host_identity_unavailable"),
        ):
            code = (
                "codex_host_identity_unavailable"
                if isinstance(value, ValueError)
                else "codex_host_changed"
            )
            with (
                self.subTest(code=code),
                mock.patch(
                    "muninn.review_managed_host.observe",
                    side_effect=(
                        value if isinstance(value, ValueError) else None
                    ),
                    return_value=(
                        value if not isinstance(value, ValueError) else None
                    ),
                ),
                mock.patch(
                    "muninn.review_managed_files.observe",
                    side_effect=AssertionError(
                        "host refusal reached package probe"
                    ),
                ),
                self.assertRaisesRegex(ValueError, code),
            ):
                review_runner.prepare_launch(
                    self.context, capability, self.prompt
                )

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=physical_host(),
            )
        )
