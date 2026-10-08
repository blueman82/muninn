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
                    "selection",
                    "interpreter",
                    "cli",
                    "total",
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
