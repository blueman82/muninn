"""Check that doctor gives safe steps for unresolved repair baselines."""

from __future__ import annotations

import json
from unittest import mock

from muninn import obs
from muninn.obs_recovery import recovery_guidance
from tests.test_obs import ReviewReceiptCase

SECRET = "private-result-and-exception-canary"


class ReviewRecoveryGuidanceTests(ReviewReceiptCase):
    """Keep recovery instructions safe and usable on supported hosts."""

    def test_health_does_not_initialize_or_repair_protocol(self) -> None:
        """Describe next steps without changing recovery files."""
        paths = (
            "review.supervisor.lock",
            "review.supervisor.json",
            "review.supervisor.repair.json",
        )
        with mock.patch.object(
            obs, "run", return_value=mock.Mock(returncode=1)
        ):
            before = obs.doctor(self.home, {"HOME": str(self.home)})
            self.assertFalse(
                any((self.home / name).exists() for name in paths)
            )
            self.assertNotIn("recovery_guidance", before)
            for name in paths:
                (self.home / name).write_text(SECRET)
                (self.home / name).chmod(0o600)
            after = obs.doctor(self.home, {"HOME": str(self.home)})
        checks = {check["check"]: check for check in after["checks"]}
        self.assertTrue(checks["unexpected_files"]["ok"])
        self.assertIsNone(checks["review_pause"]["ok"])
        self.assertIs(checks["review_repair"]["ok"], False)
        guidance = after.get("recovery_guidance")
        self.assertIsNotNone(guidance)
        assert guidance is not None
        self.assertEqual(guidance["status"], "manual_recovery_required")
        self.assertIn("Save this report", " ".join(guidance["steps"]))
        self.assertNotIn(SECRET, json.dumps(before) + json.dumps(after))
        for name in paths:
            self.assertEqual((self.home / name).read_text(), SECRET)

    def test_windows_recovery_guidance_names_the_platform_limit(self) -> None:
        """Tell Windows users why Muninn cannot repair this baseline."""
        checks: list[obs.CheckResult] = [
            {
                "check": "review_repair",
                "ok": False,
                "level": "warn",
                "detail": "baseline_present_manual_recovery_required",
            }
        ]
        guidance = recovery_guidance(checks, "win32")
        assert guidance is not None
        self.assertEqual(guidance["reason"], "windows_repair_not_qualified")
