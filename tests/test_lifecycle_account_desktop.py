"""Private CI desktops must not alter existing user objects or privileges."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.lifecycle_account_native import check_result, outer_script


class PrivateDesktopTests(unittest.TestCase):
    """Verify fixed source boundaries without claiming native success."""

    def test_already_held_impersonation_is_required_before_any_account(
        self,
    ) -> None:
        script = outer_script(Path("/synthetic"))
        boundary = script.index(
            "$privileges['caller_impersonate_present'] -ne 1"
        )
        self.assertLess(boundary, script.index("New-LocalUser"))
        self.assertNotIn("AdjustTokenPrivileges", script)
        self.assertNotIn("CreateProcessAsUserW", script)
        self.assertIn("CreateProcessWithTokenW", script)

    def test_private_objects_restore_before_launch_and_hold_until_quiescence(
        self,
    ) -> None:
        script = outer_script(Path("/synthetic"))
        self.assertIn("CWF_CREATE_ONLY", script)
        self.assertIn("D:P(A;;GA;;;", script)
        self.assertIn("SetProcessWindowStation(originalStation)", script)
        self.assertIn("SetThreadDesktop(originalDesktop)", script)
        restore = script.index("if(![MuninnCiDesktop]::Restored)")
        launch = script.index("[MuninnCiLogon]::CreateProcessWithTokenW")
        self.assertLess(restore, launch)
        cleanup = script.index("$quiescent -and [MuninnCiDesktop]::Restored")
        empty = script.index("$owned.GetInstances(0).Count -ne 0")
        self.assertLess(empty, cleanup)
        self.assertNotIn("SetUserObjectSecurity", script)
        self.assertNotIn("CreateEnvironmentBlock", script)
        self.assertIn("command.Length -gt 1024", script)
        self.assertIn("0x08000400", script)
        self.assertIn("'SystemRoot='", script)
        self.assertIn("Marshal]::FreeHGlobal($environment)", script)

    def test_process_session_metrics_require_real_matching_child_sessions(
        self,
    ) -> None:
        script = outer_script(Path("/synthetic"))
        self.assertIn("token_session", script)
        self.assertIn("ordinary_child_session", script)
        self.assertIn("scheduler_child_session", script)
        self.assertIn("GetCurrentProcess().SessionId", script)


class PrivateDesktopResultTests(unittest.TestCase):
    """Require restored objects and observed process sessions."""

    def test_missing_private_object_or_session_evidence_cannot_pass(
        self,
    ) -> None:
        report = {
            "ordinary_child_admin": 0,
            "scheduler_child_admin": 0,
            "scheduler_exit": 0,
            "scheduler_instances": 0,
            "interactive_recognized": 1,
            "account_retained": 0,
            "desktop_restored": 1,
            "private_desktop_created": 1,
            "caller_session": 0,
            "token_session": 0,
            "ordinary_child_session": 0,
            "scheduler_child_session": 0,
        }
        check_result(report)
        for key in (
            "desktop_restored",
            "private_desktop_created",
            "caller_session",
            "token_session",
            "ordinary_child_session",
            "scheduler_child_session",
        ):
            missing = dict(report)
            missing.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_result(missing)
        with self.assertRaises(ValueError):
            check_result({**report, "scheduler_child_session": 1})

    def test_restore_codes_are_captured_before_the_next_native_call(
        self,
    ) -> None:
        script = outer_script(Path("/synthetic"))
        station = script.index(
            "StationRestoreOk=SetProcessWindowStation(originalStation)"
        )
        station_error = script.index(
            "StationRestoreError=StationRestoreOk ? 0 : "
            "Marshal.GetLastWin32Error()"
        )
        desktop = script.index(
            "DesktopRestoreOk=SetThreadDesktop(originalDesktop)"
        )
        desktop_error = script.index(
            "DesktopRestoreError=DesktopRestoreOk ? 0 : "
            "Marshal.GetLastWin32Error()"
        )
        identity = script.index(
            "StationIdentityOk=GetProcessWindowStation()==originalStation"
        )
        self.assertLess(station, station_error)
        self.assertLess(station_error, desktop)
        self.assertLess(desktop, desktop_error)
        self.assertLess(desktop_error, identity)
        self.assertIn(
            "Restored=StationRestoreOk && DesktopRestoreOk &&", script
        )
        self.assertIn("StationIdentityOk && DesktopIdentityOk", script)
        for field in (
            "station_restore_ok",
            "station_restore_error",
            "desktop_restore_ok",
            "desktop_restore_error",
            "station_identity_ok",
            "desktop_identity_ok",
        ):
            self.assertIn("$report." + field, script)
