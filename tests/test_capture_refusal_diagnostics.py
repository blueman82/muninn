"""Keep bounded verification refusals useful without exposing content."""

from __future__ import annotations

from muninn import hook_learn, obs_log
from tests.test_obs import ObsCase


class CaptureRefusalDiagnosticsTests(ObsCase):
    """Exercise the real log allowlist and native capture projection."""

    def test_hook_retains_read_limit_and_sanitizes_unknown_errors(
        self,
    ) -> None:
        for code, expected in (
            ("evidence_limit", "evidence_limit"),
            ("rejected_scope", "rejected_scope"),
            ("private-exception-canary", "unavailable"),
        ):
            with self.subTest(code=code):
                hook_learn.record_capture(
                    self.home, hook_learn.HookCapture(False, (), code), {}
                )
                row = self.lines()[-1]
                self.assertEqual(row["error_code"], expected)
                self.assertFalse(row["accepted"])
                self.assertEqual(row["recovery_gaps"], 1)
                self.assertEqual(row["missed_acknowledgments"], 1)
        self.assertNotIn("private-exception-canary", self.log.read_text())

    def test_attempt_log_keeps_read_limit_without_accepting_arbitrary_text(
        self,
    ) -> None:
        for code in ("evidence_limit", "private-exception-canary"):
            obs_log.log_call(
                self.home,
                {"event": "review_attempt_failed", "error_code": code},
            )
        first, second = self.lines()
        self.assertEqual(first["error_code"], "evidence_limit")
        self.assertNotIn("error_code", second)
        self.assertNotIn("private-exception-canary", self.log.read_text())
