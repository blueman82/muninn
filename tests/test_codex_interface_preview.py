"""Check missing-old-runtime preview without live policy or cache writes."""

from __future__ import annotations

from unittest import mock

from install import codex_interface_preview as previewing
from install.context import StepFailedError
from muninn import review_codex_policy
from tests.host_renewal_support import HostFixture, states
from tests.managed_codex_support import identity, physical_host


class InterfacePreviewTests(HostFixture):
    """Qualify only fake current identities against real temporary metadata."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx.review_policy = None
        self.ctx.renew_codex_host = None
        self.physical = physical_host()
        self.enterContext(
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=self.physical,
            )
        )
        self.enterContext(
            mock.patch.object(
                review_codex_policy,
                "host_identity",
                return_value=self.old_host,
            )
        )
        self.observed = identity(self.home / ".codex/packages/standalone")
        self.enterContext(
            mock.patch(
                "muninn.review_managed_files.observe",
                return_value=self.observed,
            )
        )
        self.enterContext(
            mock.patch(
                "muninn.review_managed_probe.negotiate",
                return_value=self.observed,
            )
        )

    def test_missing_old_executable_can_preview_stable_rule_without_writes(
        self,
    ) -> None:
        self.request()
        self.executable.unlink()
        before = states(self.home)
        plan = previewing.preview(self.ctx)
        self.assertEqual(plan.old, self.old)
        self.assertEqual(plan.observation.requested.version, "0.166.0")
        self.assertEqual(
            plan.candidate.model_dump(
                exclude={"runner", "experimental_codex"}
            ),
            self.old.model_dump(exclude={"runner", "experimental_codex"}),
        )
        self.assertEqual(len(plan.bridge.requests), 1)
        self.assertEqual(states(self.home), before)
        self.assertEqual(previewing.preview(self.ctx).digest, plan.digest)

    def test_unknown_current_interface_refuses_before_authority(self) -> None:
        with (
            mock.patch(
                "muninn.review_managed_probe.negotiate",
                side_effect=ValueError("runtime_interface_unsupported"),
            ),
            self.assertRaisesRegex(
                StepFailedError, "runtime_interface_unsupported"
            ),
        ):
            previewing.preview(self.ctx)

    def test_missing_saved_proof_is_not_an_auth_fallback(self) -> None:
        for path in (self.ctx.data / "review-capabilities").glob("*.json"):
            path.unlink()
        with self.assertRaises(StepFailedError):
            previewing.preview(self.ctx)
