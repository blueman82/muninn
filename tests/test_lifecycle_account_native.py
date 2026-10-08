"""The account spike keeps ordinary interactive task ownership explicit."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import lifecycle_account_native, lifecycle_account_scripts


class AccountSpikeTests(unittest.TestCase):
    """Validate fixed scripts without claiming native process execution."""

    def test_scripts_use_retained_interactive_token_and_exact_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            script = lifecycle_account_scripts.outer_script(parent)
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
            source = lifecycle_account_scripts.outer_script(Path(temporary))
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

    def test_outer_seams_report_codes_without_hiding_native_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = lifecycle_account_scripts.outer_script(Path(temporary))
        self.assertLess(
            source.index("$outerStage=1;try {"), source.index("Add-Type")
        )
        self.assertLess(
            source.index("$outerStage=2"), source.index("$process=New-Object")
        )
        self.assertIn("$outerStage=3", source)
        self.assertIn("$outerStage=4", source)
        self.assertIn("$report.outer_stage=$outerStage", source)
        self.assertIn("$probeFailure=$_.Exception.GetBaseException()", source)
        self.assertIn("ConvertTo-Json -Compress;exit 1", source)
        self.assertNotIn("$report.message", source)

    def test_child_seams_keep_native_errors_and_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = lifecycle_account_scripts.outer_script(Path(temporary))
        stages = [
            source.index(f"$report.child_stage={value}")
            for value in range(1, 6)
        ]
        self.assertEqual(stages, sorted(stages))
        launch = source.index(
            "$launched=[MuninnCiLogon]::CreateProcessWithTokenW"
        )
        failure = source.index("if(!$launched)", launch)
        last_error = source.index("GetLastWin32Error()", failure)
        launch_metric = source.index(
            "$report.child_launch_ok=[int]$launched", failure
        )
        self.assertLess(stages[0], launch)
        self.assertLess(last_error, launch_metric)
        wait = source.index("$wait=[MuninnCiLogon]::WaitForSingleObject")
        self.assertLess(stages[1], wait)
        self.assertIn("($process.process,60000)", source[wait:])
        wait_error = source.index("GetLastWin32Error()", wait)
        wait_metric = source.index("$report.child_wait_result=$wait", wait)
        self.assertLess(wait_error, wait_metric)
        self.assertIn("if($wait -eq -1)", source[wait:wait_metric])
        self.assertIn("if($wait -ne 0)", source[wait_metric:])
        self.assertIn("throw 'ordinary_child_exit_unproven'", source)
        query = source.index("$queried=[MuninnCiLogon]::GetExitCodeProcess")
        self.assertLess(stages[2], query)
        query_error = source.index("GetLastWin32Error()", query)
        query_metric = source.index(
            "$report.child_exit_query_ok=[int]$queried", query
        )
        self.assertLess(query_error, query_metric)
        self.assertIn("$report.child_exit_code=$exitCode", source[query:])
        self.assertIn("if($exitCode -ne 0)", source[query:])
        self.assertIn("throw 'ordinary_child_exit_failed'", source)
        child_report = source.index("$child=Join-Path")
        self.assertLess(stages[3], child_report)
        self.assertLess(stages[4], source.index("Test-Owned $owned $base"))
        self.assertIn("$quiescent=$false\n $report.child_stage=2", source)
        self.assertNotIn("TerminateProcess", source)

    def test_nonzero_outer_json_is_recorded_then_original_exit_is_raised(
        self,
    ) -> None:
        report = {
            "phase": "account_create",
            "account_retained": 1,
            "outer_stage": 4,
            "winerror": 123,
        }
        with tempfile.TemporaryDirectory() as temporary:
            handle = SimpleNamespace(name=temporary)
            result = subprocess.CompletedProcess(
                [], 7, json.dumps(report).encode(), b""
            )
            with (
                patch.object(
                    lifecycle_account_native,
                    "sys",
                    SimpleNamespace(platform="win32"),
                ),
                patch.object(
                    lifecycle_account_native.tempfile,
                    "TemporaryDirectory",
                    return_value=handle,
                ),
                patch.object(
                    lifecycle_account_native,
                    "noop_definition",
                    return_value=b"fixture",
                ),
                patch.object(
                    lifecycle_account_native,
                    "powershell",
                    return_value=["powershell.exe"],
                ),
                patch.object(
                    lifecycle_account_native.subprocess,
                    "run",
                    return_value=result,
                ),
                patch.object(lifecycle_account_native, "write") as write,
                patch("builtins.print"),
                self.assertRaises(subprocess.CalledProcessError) as raised,
            ):
                lifecycle_account_native.main()
            self.assertEqual(raised.exception.returncode, 7)
            native = write.call_args_list[1]
            self.assertEqual(native.kwargs["metrics"]["outer_stage"], 4)
            self.assertEqual(native.kwargs["error"].errno, 123)
            self.assertEqual(
                write.call_args_list[-1].kwargs["metrics"]["outer_returncode"],
                7,
            )
