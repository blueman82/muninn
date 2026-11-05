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
from tests.managed_codex_support import identity, selection
from tests.test_review_codex_launch import CodexProductionLaunchTests


class ManagedLaunchTests(CodexProductionLaunchTests):
    """Use only temporary metadata and mocked external runtime observation."""

    def managed(self) -> tuple[ManagedCapability, ManagedInterfaceBinding]:
        """Publish an approved fixture without touching real HOME."""
        selected = selection(self.tmp / "manager")
        self.policy = self.policy.model_copy(update={"runner": selected})
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
            host_identity="1" * 64,
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
            host_identity="1" * 64,
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
