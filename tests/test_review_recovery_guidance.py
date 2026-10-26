"""Check that doctor gives safe steps for unresolved repair baselines."""

from __future__ import annotations

import json
import sys
from unittest import mock

from muninn import obs, obs_status
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
        expected_status = (
            "manual_recovery_required"
            if sys.platform == "win32"
            else "recovery_check_required"
        )
        self.assertEqual(guidance["status"], expected_status)
        if sys.platform == "win32":
            self.assertIn(
                "contact your Muninn maintainer",
                " ".join(guidance["steps"]),
            )
        else:
            self.assertIn("muninn recover", " ".join(guidance["steps"]))
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
                "detail": "baseline_present_recovery_unverified",
            }
        ]
        guidance = recovery_guidance(checks, "win32")
        assert guidance is not None
        self.assertEqual(guidance["status"], "manual_recovery_required")
        self.assertEqual(guidance["reason"], "windows_repair_not_qualified")
        self.assertIn(
            "contact your Muninn maintainer", " ".join(guidance["steps"])
        )


class RecoveryObservationTests(ReviewReceiptCase):
    """Never infer safe automatic recovery from a protocol path."""

    def test_missing_baseline_has_no_repair_guidance(self) -> None:
        checks = [
            obs.CheckResult(check=name, ok=ok, detail=detail, level=level)
            for name, ok, detail, level in obs_status.review_protocol(
                self.home
            )
        ]
        self.assertIsNone(recovery_guidance(checks, "darwin"))

    def test_unverified_paths_only_request_checked_recovery(self) -> None:
        for kind in ("malformed", "directory", "symlink", "public"):
            home = self.home / kind
            home.mkdir()
            path = home / "review.supervisor.repair.json"
            with self.subTest(kind=kind):
                if kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    path.symlink_to(home / "missing-target")
                else:
                    path.write_text(SECRET)
                    path.chmod(0o644 if kind == "public" else 0o600)
                before = path.lstat()
                checks = [
                    obs.CheckResult(
                        check=name, ok=ok, detail=detail, level=level
                    )
                    for name, ok, detail, level in obs_status.review_protocol(
                        home
                    )
                ]
                repair = next(
                    c for c in checks if c["check"] == "review_repair"
                )
                self.assertEqual(
                    repair["detail"], "baseline_present_recovery_unverified"
                )
                for platform in ("darwin", "linux"):
                    guidance = recovery_guidance(checks, platform)
                    assert guidance is not None
                    self.assertEqual(
                        guidance["status"], "recovery_check_required"
                    )
                    self.assertIn("not verified", " ".join(guidance["steps"]))
                    self.assertIn(
                        "legacy_volume_confirmation_required",
                        " ".join(guidance["steps"]),
                    )
                    self.assertNotIn(SECRET, json.dumps(guidance))
                self.assertEqual(path.lstat(), before)
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()

    def test_unsupported_platform_requires_maintainer(self) -> None:
        checks = [
            obs.CheckResult(
                check="review_repair",
                ok=False,
                level="warn",
                detail="baseline_present_recovery_unverified",
            )
        ]
        guidance = recovery_guidance(checks, "freebsd")
        assert guidance is not None
        self.assertEqual(guidance["status"], "manual_recovery_required")
        self.assertEqual(guidance["reason"], "recovery_platform_unqualified")
