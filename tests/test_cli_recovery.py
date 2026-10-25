"""Recover an interrupted maintenance pause through the supported CLI."""

from __future__ import annotations

import json
import os
from unittest import mock

from muninn import cli_recovery, review_pause
from muninn import review_pause_state as state
from tests.cli_support import CliCase
from tests.review_pause_support import initialize

BOOT_A = "linux:12345678-1234-4234-8234-123456789abc"
BOOT_B = "linux:87654321-1234-4234-8234-123456789abc"


class CliRecoveryTests(CliCase):
    """Keep automated recovery bounded by existing proof checks."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)

    def leave_uncertain_owner(self) -> None:
        """Model a process that ended before it completed its pause record."""
        fd, _ = state.begin_owner(self.home)
        os.close(fd)

    def test_recover_waits_for_reboot_then_repairs_verified_linux_state(
        self,
    ) -> None:
        self.leave_uncertain_owner()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_A,
            ),
        ):
            code, first, _ = self.muninn("recover")
        self.assertEqual((code, first["reason"]), (2, "reboot_required"))
        self.assertIn("until the computer has rebooted", first["why"])
        self.assertIn("run `muninn recover` again", first["next_action"])
        self.assertTrue((self.home / state.BASELINE).is_file())
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_B,
            ),
        ):
            code, second, _ = self.muninn("recover")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["recovery"]["status"], "recovered")
        self.assertEqual(second["recovery"]["repair_phase"], "complete")
        self.assertIn(
            "system service manager", second["recovery"]["next_action"]
        )
        self.assertFalse((self.home / state.BASELINE).exists())
        witness = json.loads((self.home / state.WITNESS).read_text())
        self.assertEqual(witness["state"], "complete")

    def test_windows_refuses_without_changing_repair_evidence(self) -> None:
        self.leave_uncertain_owner()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_A,
            ),
        ):
            self.muninn("recover")
        baseline = (self.home / state.BASELINE).read_bytes()
        witness = (self.home / state.WITNESS).read_bytes()
        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="win32"
        ):
            code, out, _ = self.muninn("recover")
        self.assertEqual(
            (code, out["reason"]), (2, "windows_repair_unqualified")
        )
        self.assertIn("not qualified", out["why"])
        self.assertIn("contact your Muninn maintainer", out["next_action"])
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), witness)

    def test_healthy_home_reports_no_recovery_needed(self) -> None:
        code, out, _ = self.muninn("recover")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["recovery"]["status"], "no_recovery_needed")

    def test_empty_data_home_reports_no_recovery_needed(self) -> None:
        empty = self.tmp / "empty-home"
        empty.mkdir(mode=0o700)
        code, out, _ = self.muninn("recover", env={"MUNINN_HOME": str(empty)})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["recovery"]["status"], "no_recovery_needed")
