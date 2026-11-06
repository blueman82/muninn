"""Enroll a checked physical host without trusting a legacy name digest."""

from __future__ import annotations

from unittest import mock

from install.codex_interface_preview import preview, summary
from tests.host_renewal_support import states
from tests.test_codex_interface_preview import InterfacePreviewTests


class InterfaceHostTests(InterfacePreviewTests):
    """Keep original evidence while proposing one explicit host enrollment."""

    def test_changed_legacy_host_and_missing_runtime_can_preview(self) -> None:
        self.request()
        self.executable.unlink()
        before = states(self.home)
        with mock.patch(
            "muninn.review_codex_policy.host_identity",
            return_value=self.current_host,
        ):
            plan = preview(self.ctx)
        report = summary(plan)
        self.assertTrue(report["host_binding_changed"])
        self.assertEqual(report["predecessor_host_continuity"], "unverified")
        self.assertEqual(report["saved_legacy_host"], self.old_host)
        self.assertEqual(report["observed_legacy_host"], self.current_host)
        self.assertEqual(states(self.home), before)
