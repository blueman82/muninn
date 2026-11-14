"""Build a failed bridged call using isolated managed-runner fixtures."""

from __future__ import annotations

import unittest

from muninn import (
    review_budget,
    review_codex_launch,
    review_requests,
    review_unknown_recovery,
)
from muninn.review_codex_policy import CodexExperiment
from muninn.review_models import ReviewContext, ReviewPolicy
from muninn.review_runtime_models import require_legacy_runner
from muninn.review_unknown_state import UnknownAcknowledgement
from tests import test_review_managed_supervisor as managed_tests
from tests.managed_codex_support import identity
from tools.codex_policy_artifacts import plan_artifacts, publish_artifacts


class UnknownRecoveryCase(unittest.TestCase):
    """Provide one never-launched historical unknown and checked authority."""

    def setUp(self) -> None:
        self.fixture = managed_tests.ManagedSupervisorTests()
        self.fixture.policy_revision = 3
        self.fixture.carry_legacy = True
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        fixture = self.fixture
        pending = fixture.supervisor._claim(13.0)
        assert pending is not None
        self.context: ReviewContext = pending.context
        execution = identity(fixture.root)
        fixture.transaction(
            lambda: review_budget.reserve_call(
                fixture.conn, self.context, 13.0, execution=execution
            )
        )
        fixture.transaction(
            lambda: review_requests.fail_attempt(
                fixture.conn, self.context.attempt_id, "config", 14.0
            )
        )
        fixture.supervisor.stop(15.0)
        fixture.conn.execute("UPDATE review_call SET state='unknown'")
        fixture.conn.execute("UPDATE review_control SET admission=0")

    def recover(self) -> UnknownAcknowledgement:
        """Run the actual maintenance entry point without native model work."""
        return review_unknown_recovery.recover(
            self.fixture.home,
            self.context.attempt_id,
            1,
            accept_uncertainty=True,
            now=20.0,
        )

    def publish_legacy_authority(self, policy: ReviewPolicy) -> None:
        """Build legacy authority with the production artifact recipe."""
        assert isinstance(policy.experimental_codex, CodexExperiment)
        auth = self.fixture.home.parent / "fake-managed-auth"
        binding = review_codex_launch.ExperimentalBinding(
            requested=require_legacy_runner(policy.runner),
            host_identity=policy.experimental_codex.host_identity,
            executable_sha256="a" * 64,
            output_contract_digest=review_codex_launch.output_contract_digest(),
            cli_cwd=self.fixture.home / "review-runner-cwd",
            auth_home=auth,
            schema_path=self.fixture.home / "review-codex-schema.json",
        )
        prompt = (
            self.fixture.home
            / "review-prompts"
            / (policy.prompt_digest + ".json")
        ).read_bytes()
        plan = plan_artifacts(
            policy, prompt, binding, review_codex_launch.fingerprint(binding)
        )
        publish_artifacts(self.fixture.home, plan)
