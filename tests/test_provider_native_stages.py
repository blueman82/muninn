"""Synthetic launcher stage probes retain source guards and fixed output."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.context import Ctx, run_real
from muninn import platform_windows
from tests.ingest_support import ROOT
from tests.provider_native_stages import (
    launcher_stages,
    parse_stages,
    timed_launcher,
)


class LauncherStagesTest(unittest.TestCase):
    """Only fixed numeric timings are exported after actual route failure."""

    def test_instrumented_copy_retains_guards_and_exit_behavior(self) -> None:
        source = (ROOT / "bin/muninn.ps1").read_text()
        result = timed_launcher(source)
        for line in source.splitlines():
            self.assertIn(line, result)
        for name in (
            "initialize",
            "ancestry",
            "selection",
            "interpreter",
            "cli",
        ):
            self.assertIn("stage_" + name + "_ms", result)
        for name in (
            "open",
            "acl_enter",
            "identity",
            "translate",
            "acl_exit",
            "native_enter",
            "native_create",
            "native_metadata",
            "native_path",
            "hold_enter",
            "before_open",
        ):
            field = "stage_first_" + name + "_ms"
            self.assertEqual(result.count("Contains('" + field + "')"), 1)
        for start, end in (
            ("        if ($handle.IsInvalid)", "        $attributes ="),
            ("            if ($native::GetFileType", "            $flags ="),
            ("        if ($a -eq 0", "        $resolved ="),
        ):
            self.assertIn(
                source[source.index(start) : source.index(end)], result
            )
        self.assertIn("exit $code", result)
        self.assertIn("$guards[$index].Dispose()", result)
        self.assertNotIn("Add-Type", result)
        for bad in ("", source.replace("    $native = Initialize-Native", "")):
            with self.assertRaises(ValueError):
                timed_launcher(bad)

    def test_parser_accepts_only_all_fixed_numeric_markers(self) -> None:
        values = {
            "stage_initialize_ms": 1,
            "stage_ancestry_ms": 2,
            "stage_selection_ms": 3,
            "stage_interpreter_ms": 4,
            "stage_cli_ms": 5,
            "stage_first_open_ms": 1,
            "stage_first_acl_enter_ms": 1,
            "stage_first_identity_ms": 1,
            "stage_first_translate_ms": 1,
            "stage_first_acl_exit_ms": 1,
            "stage_first_native_enter_ms": 1,
            "stage_first_native_create_ms": 1,
            "stage_first_native_metadata_ms": 1,
            "stage_first_native_path_ms": 1,
            "stage_first_hold_enter_ms": 1,
            "stage_first_before_open_ms": 1,
        }
        raw = b"__MUNINN_STAGE__" + json.dumps(values).encode() + b"\n"
        self.assertEqual(parse_stages(raw), values)
        for bad in (
            b"",
            b"__MUNINN_STAGE__{}",
            b"__MUNINN_STAGE__private malformed body",
            b"__MUNINN_STAGE__" + b"x" * 4097,
            b"__MUNINN_STAGE__" + json.dumps({**values, "path": 1}).encode(),
            b"__MUNINN_STAGE__"
            + json.dumps({**values, "stage_cli_ms": True}).encode(),
            raw + raw,
        ):
            with self.assertRaises(ValueError):
                parse_stages(bad)

    def test_fresh_copy_uses_same_root_arguments_environment_and_stdin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "synthetic", platform="win32")
            launcher = ctx.muninn.with_suffix(".ps1")
            argv = [
                "trusted-powershell.exe",
                "-File",
                str(launcher),
                "hook",
                "claude-start",
            ]
            values = {
                "stage_" + key + "_ms": 1
                for key in (
                    "initialize",
                    "ancestry",
                    "selection",
                    "interpreter",
                    "cli",
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
            with (
                mock.patch(
                    "tests.provider_native_stages.sys.platform", "win32"
                ),
                mock.patch.object(
                    platform_windows, "assert_executable", create=True
                ),
                mock.patch(
                    "tests.provider_native_stages.configedit.atomic_write"
                ) as write,
                mock.patch(
                    "tests.provider_native_stages.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        b"discard private stdout",
                        b"__MUNINN_STAGE__" + json.dumps(values).encode(),
                    ),
                ) as run,
            ):
                env = {"HOME": "synthetic"}
                result = launcher_stages(ctx, env, argv, b"synthetic stdin")
            target = write.call_args.args[0]
            self.assertEqual(target.parent, launcher.parent)
            self.assertNotEqual(target, launcher)
            called = run.call_args
            self.assertEqual(
                called.args[0], [*argv[:2], str(target), *argv[3:]]
            )
            self.assertEqual(called.kwargs["env"], env)
            self.assertEqual(called.kwargs["input"], b"synthetic stdin")
            self.assertEqual(result["stage_error"], 0)
            self.assertEqual(result["stage_returncode"], 0)

    def test_non_windows_and_runtime_failure_do_not_export_streams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "synthetic", platform="win32")
            argv = [
                "trusted-powershell.exe",
                "-File",
                str(ctx.muninn.with_suffix(".ps1")),
            ]
            with (
                mock.patch(
                    "tests.provider_native_stages.sys.platform", "darwin"
                ),
                mock.patch(
                    "tests.provider_native_stages.subprocess.run"
                ) as run,
            ):
                self.assertEqual(launcher_stages(ctx, {}, argv, b""), {})
                run.assert_not_called()
            with (
                mock.patch(
                    "tests.provider_native_stages.sys.platform", "win32"
                ),
                mock.patch.object(
                    platform_windows, "assert_executable", create=True
                ),
                mock.patch(
                    "tests.provider_native_stages.configedit.atomic_write"
                ),
                mock.patch(
                    "tests.provider_native_stages.subprocess.run",
                    side_effect=OSError("private body"),
                ),
            ):
                result = launcher_stages(ctx, {}, argv, b"")
            self.assertEqual(result["stage_error"], 1)
            self.assertNotIn("private", json.dumps(result))
