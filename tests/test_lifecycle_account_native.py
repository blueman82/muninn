"""The account spike keeps ordinary interactive task ownership explicit."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tests import lifecycle_account_native


class AccountSpikeTests(unittest.TestCase):
    """Validate fixed scripts without claiming native process execution."""

    def test_scripts_use_retained_interactive_token_and_exact_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            script = lifecycle_account_native.outer_script(parent)
        self.assertIn("LogonUserW", script)
        self.assertIn("2,0", script)
        self.assertIn("CreateProcessWithTokenW", script)
        self.assertIn("WaitForSingleObject", script)
        self.assertIn("$password=$null", script)
        self.assertIn("SetAccessRuleProtection($true,$false)", script)
        self.assertIn("$user.SID.Value", script)
        self.assertIn("$created -and $quiescent", script)

    def test_noop_definition_keeps_safe_native_task_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            xml = lifecycle_account_native.noop_definition(Path(temporary))
        root = ET.fromstring(xml)
        namespace = {
            "t": "http://schemas.microsoft.com/windows/2004/02/mit/task"
        }
        for path, value in (
            ("t:Principals/t:Principal/t:LogonType", "InteractiveToken"),
            ("t:Principals/t:Principal/t:RunLevel", "LeastPrivilege"),
            ("t:Settings/t:AllowHardTerminate", "false"),
            ("t:Settings/t:ExecutionTimeLimit", "PT0S"),
        ):
            self.assertEqual(root.findtext(path, namespaces=namespace), value)
        command = root.findtext(
            "t:Actions/t:Exec/t:Command", namespaces=namespace
        )
        assert command is not None
        self.assertTrue(command.endswith("powershell.exe"))

    def test_report_requires_actual_both_children_and_quiescence(self) -> None:
        passed = {
            "ordinary_child_admin": 0,
            "scheduler_child_admin": 0,
            "scheduler_exit": 0,
            "scheduler_instances": 0,
            "interactive_recognized": 1,
            "account_retained": 0,
            "desktop_restored": 1,
            "private_desktop_created": 1,
        }
        sessions = {
            "caller_session": 0,
            "token_session": 0,
            "ordinary_child_session": 0,
            "scheduler_child_session": 0,
        }
        lifecycle_account_native.check_result({**passed, **sessions})
        with self.assertRaises(ValueError):
            lifecycle_account_native.check_result(
                {**passed, **sessions, "winerror": 1314}
            )
        for key in passed:
            with self.subTest(key=key), self.assertRaises(ValueError):
                lifecycle_account_native.check_result(
                    {**passed, **sessions, key: passed[key] + 1}
                )

    def test_ps5_and_independent_cleanup_checks_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = lifecycle_account_native.outer_script(Path(temporary))
        self.assertNotIn("-AsHashtable", source)
        self.assertNotIn("CreateEnvironmentBlock", source)
        child_exit = source.index("$child=Join-Path")
        owned_check = source.index("Test-Owned $owned $base")
        empty_check = source.index("$owned.GetInstances(0).Count -ne 0")
        quiescence = source.index(
            "$quiescent=$report.scheduler_instances -eq 0"
        )
        remove = source.index("Remove-LocalUser -SID")
        self.assertLess(child_exit, owned_check)
        disabled = source.index("$owned.Enabled=$false")
        self.assertLess(owned_check, disabled)
        self.assertLess(disabled, empty_check)
        self.assertLess(empty_check, quiescence)
        self.assertLess(quiescence, remove)
        self.assertIn("catch {$report.account_retained=1", source)
        self.assertIn("Actions/Exec/Arguments", source)
        self.assertIn("Settings/AllowHardTerminate", source)
