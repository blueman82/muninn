"""Fixed provider and ordinary-account diagnostic codes expose no bodies."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.native_diagnostics import write


class NativeDiagnosticCodesTest(unittest.TestCase):
    """Finite code observations retain privacy and reject unknown fields."""

    def test_private_desktop_session_metrics_are_numeric_only(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            metrics = {
                "desktop_restored": 1,
                "station_restore_ok": 1,
                "station_restore_error": 0,
                "desktop_restore_ok": 1,
                "desktop_restore_error": 0,
                "station_identity_ok": 1,
                "desktop_identity_ok": 1,
                "desktop_identity_before": 1,
                "desktop_restore_called": 0,
                "private_desktop_created": 1,
                "token_session": 2,
                "caller_session": 2,
                "ordinary_child_session": 2,
                "scheduler_child_session": 2,
            }
            write("ordinary", "account_child_start", metrics=metrics)
            stages = {
                "stage_" + name + "_ms": 1
                for name in (
                    "initialize",
                    "root",
                    "installed",
                    "first_before_hold",
                    "ancestry",
                    "selection_base",
                    "selection_metadata",
                    "selection_json",
                    "selection_fields",
                    "selection_release",
                    "selection",
                    "interpreter",
                    "cli",
                    "total",
                    "first_ordinary_before_drive",
                    "first_ordinary_drive",
                    "first_ordinary_item",
                    "first_open",
                    "first_acl_enter",
                    "first_identity",
                    "first_translate",
                    "first_acl_exit",
                    "first_native_enter",
                    "first_native_create",
                    "first_native_metadata",
                    "first_native_path",
                    "first_hold_enter",
                    "first_before_open",
                )
            }
            write(
                "provider",
                "routes",
                metrics={**stages, "stage_returncode": 0, "stage_error": 0},
            )
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            data = json.loads(path.read_text())
            for key, value in metrics.items():
                self.assertEqual(data[key], value)
            for key in (
                "desktop_name",
                "window_station",
                "token_session_bytes",
            ):
                with self.assertRaises(ValueError):
                    write("ordinary", "account_child_start", metrics={key: 1})

    def test_ordinary_bootstrap_codes_do_not_export_captured_stderr(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            metrics = {
                "outer_returncode": 1,
                "outer_compile_code": 1009,
                "outer_parser_error": 0,
                "outer_stage": 3,
            }
            write("ordinary", "account_create", metrics=metrics)
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            data = json.loads(path.read_text())
            for key, value in metrics.items():
                self.assertEqual(data[key], value)
            with self.assertRaises(ValueError):
                write(
                    "ordinary",
                    "account_create",
                    metrics={"captured_stderr": 1},
                )

    def test_child_launch_codes_do_not_export_native_handles(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            metrics = {
                "child_stage": 1,
                "child_launch_ok": 0,
                "child_wait_result": 258,
                "child_exit_query_ok": 0,
                "child_exit_code": 1,
            }
            write("ordinary", "account_child_start", metrics=metrics)
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            data = json.loads(path.read_text())
            for key, value in metrics.items():
                self.assertEqual(data[key], value)
            with self.assertRaises(ValueError):
                write(
                    "ordinary",
                    "account_child_start",
                    metrics={"child_handle": 1},
                )

    def test_child_failure_codes_refuse_script_and_report_bodies(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            metrics = {
                "child_parse_count": 0,
                "child_parse_line": 0,
                "child_report_seen": 1,
                "child_failure_stage": 2,
                "child_failure_hresult": -2146233087,
                "child_failure_line": 9,
            }
            write("ordinary", "account_child_start", metrics=metrics)
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            data = json.loads(path.read_text())
            for key, value in metrics.items():
                self.assertEqual(data[key], value)
            for key in ("child_script", "child_report", "child_exception"):
                with self.assertRaises(ValueError):
                    write("ordinary", "account_child_start", metrics={key: 1})

    def test_child_module_codes_do_not_export_names_or_error_bodies(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            metrics = {
                "child_module_readable": 0,
                "child_module_hresult": -2147024891,
                "child_error_category": 1,
                "child_command_type": -1,
            }
            write("ordinary", "account_child_start", metrics=metrics)
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            data = json.loads(path.read_text())
            for key, value in metrics.items():
                self.assertEqual(data[key], value)
            for key in ("child_module_name", "child_error_body"):
                with self.assertRaises(ValueError):
                    write("ordinary", "account_child_start", metrics={key: 1})

    def test_child_exception_kind_is_a_fixed_numeric_code(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-ordinary-proof.json"
            )
            for value in range(-1, 6):
                write(
                    "ordinary",
                    "account_child_start",
                    metrics={"child_exception_kind": value},
                )
                self.assertEqual(
                    json.loads(path.read_text())["child_exception_kind"], value
                )
            for value in (-2, 6, True):
                with self.assertRaises(ValueError):
                    write(
                        "ordinary",
                        "account_child_start",
                        metrics={"child_exception_kind": value},
                    )
            for key in ("child_exception_name", "child_exception_body"):
                with self.assertRaises(ValueError):
                    write("ordinary", "account_child_start", metrics={key: 1})
