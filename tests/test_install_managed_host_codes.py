"""Keep exact finite physical causes on explicit managed learning restart."""

from __future__ import annotations

from unittest import mock

from install import learning_policy_recovery
from install.context import StepFailedError
from tests.test_review_runtime_bridge import RuntimeBridgeTests


class ManagedRestartHostCodesTests(RuntimeBridgeTests):
    """Exercise the existing checked managed restart entry helper."""

    def test_explicit_restart_keeps_both_physical_codes(self) -> None:
        for code in ("codex_host_identity_unavailable", "codex_host_changed"):
            with (
                self.subTest(code=code),
                mock.patch(
                    "install.learning_policy_recovery.check_host",
                    side_effect=ValueError(code),
                ),
                self.assertRaisesRegex(StepFailedError, "^" + code + "$"),
            ):
                learning_policy_recovery.check_managed(
                    self.conn, self.home, self.new
                )

    def test_other_errors_keep_the_generic_restart_code(self) -> None:
        for error in (
            ValueError("private details"),
            OSError("codex_host_changed"),
        ):
            with (
                self.subTest(kind=type(error)),
                mock.patch(
                    "install.learning_policy_recovery.check_host",
                    side_effect=error,
                ),
                self.assertRaisesRegex(
                    StepFailedError, "^learning_recovery_policy_required$"
                ),
            ):
                learning_policy_recovery.check_managed(
                    self.conn, self.home, self.new
                )
