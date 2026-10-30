"""Keep native child command failures useful without exporting private text."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from unittest import mock

from install import lifecycle, preflight
from install.context import Ctx, StepFailedError, must
from tests import (
    lifecycle_native,
    lifecycle_native_failure,
    lifecycle_native_windows,
)
from tests.native_diagnostics import transfer, write

CANARY = "private-command-argument-stderr-environment-canary"


def failed_run(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Return an actual nonzero command result with private output."""
    return subprocess.CompletedProcess(
        argv, -23, CANARY.encode(), CANARY.encode()
    )


def caught_codes(action: Callable[[], object]) -> tuple[dict[str, int], str]:
    """Read diagnostics inside the catch before unittest clears traceback."""
    try:
        action()
    except StepFailedError as error:
        return lifecycle_native_failure.failure_codes(error), str(error)
    raise AssertionError("must unexpectedly accepted nonzero exit")


class LifecycleFailureTests(unittest.TestCase):
    """Exercise the real must, child catch, and parent artifact transfer."""

    def test_command_failure_survives_child_and_parent_failure_paths(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as child,
            tempfile.TemporaryDirectory() as outer,
        ):
            parent = Path(child)
            ctx = Ctx(parent, failed_run, "failure")

            def exercise(_parent: Path) -> dict[str, object]:
                write("lifecycle", "upgrade_install")
                preflight._git(ctx, parent, "show", CANARY)
                raise AssertionError("must unexpectedly accepted nonzero exit")

            with (
                mock.patch.dict(
                    "os.environ", {"MUNINN_NATIVE_SCRATCH": child}
                ),
                mock.patch.object(lifecycle_native, "exercise", exercise),
                mock.patch.object(
                    lifecycle_native_windows, "token_codes", return_value={}
                ),
                mock.patch.object(
                    lifecycle_native.lifecycle, "_identity", return_value="sid"
                ),
                mock.patch.object(lifecycle_native, "run_real", failed_run),
                mock.patch.object(
                    lifecycle_native,
                    "parse_process",
                    return_value={"pid": 7, "created": "synthetic"},
                ),
                mock.patch.object(
                    lifecycle_native.lifecycle_native_records,
                    "record_task_mismatch",
                    return_value=[],
                ),
                mock.patch.object(
                    lifecycle_native.lifecycle_native_records,
                    "record_writer_mismatch",
                    return_value=[],
                ),
                mock.patch.object(
                    lifecycle_native.lifecycle_native_records,
                    "record_new_job",
                    return_value=[7],
                ),
                mock.patch.object(
                    lifecycle_native.lifecycle_native_records,
                    "record_doctor_output",
                    return_value=[1, 123, 0, 0, 0],
                ),
            ):
                self.assertEqual(lifecycle_native._child(parent), 1)
            self.assertEqual(
                json.loads((parent / "result.json").read_text())["ok"], False
            )
            with (
                mock.patch.dict(
                    "os.environ", {"MUNINN_NATIVE_SCRATCH": outer}
                ),
                mock.patch.object(
                    lifecycle_native_windows,
                    "_outer_task",
                    return_value=(ctx, "synthetic", parent / "outer.xml"),
                ),
                mock.patch.object(lifecycle_native_windows, "_create_task"),
                mock.patch.object(
                    lifecycle_native_windows.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "ordinary child lifecycle failed"
                ):
                    lifecycle_native_windows.run_child(parent, parent)
                write("lifecycle", None, error=RuntimeError(CANARY))
            output = (
                Path(outer)
                / "muninn-native-diagnostics"
                / "muninn-lifecycle-proof.json"
            ).read_text()
            report = json.loads(output)
            self.assertEqual(report.get("step_command_family"), 1)
            self.assertEqual(report["step_callsite"], 1)
            self.assertEqual(report["step_exit_status"], -23)
            self.assertEqual(report["phase"], "upgrade_install")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error_type"], "StepFailedError")
            self.assertEqual(report["new_job_flags"], 7)
            self.assertIsInstance(report["step_failure_id"], int)
            self.assertNotIn(CANARY, output)
            self.assertNotIn(child, output)

    def test_must_preserves_nonzero_failure_and_quiet_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), failed_run, "failure")
            for quiet in (False, True):
                codes, message = caught_codes(
                    partial(must, ctx, ["git", "show", CANARY], quiet=quiet)
                )
                self.assertEqual(CANARY in message, not quiet)
                self.assertIn("exited -23", message)
                self.assertEqual(
                    codes,
                    {
                        "step_command_family": 1,
                        "step_callsite": 0,
                        "step_exit_status": -23,
                    },
                )

    def test_windows_callsite_uses_real_must_and_unsigned_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.CompletedProcess(
                [], 4294967295, b"", CANARY.encode()
            )
            ctx = Ctx(Path(tmp), mock.Mock(return_value=result), "failure")
            codes, _message = caught_codes(lambda: lifecycle._identity(ctx))
            self.assertEqual(
                codes,
                {
                    "step_command_family": 2,
                    "step_callsite": 9,
                    "step_exit_status": 4294967295,
                },
            )

    def test_unknown_commands_remain_unknown_and_unrelated_errors_stay_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), failed_run, "failure")
            for command, action, family in (
                (CANARY, CANARY, 0),
                (r"C:\private\schtasks.EXE", "/Create", 10),
                (r"C:\private\schtasks.EXE", CANARY, 9),
            ):
                codes, _message = caught_codes(
                    partial(must, ctx, [command, action], quiet=True)
                )
                self.assertEqual(
                    codes,
                    {
                        "step_command_family": family,
                        "step_callsite": 0,
                        "step_exit_status": -23,
                    },
                )
            for error in (RuntimeError(CANARY), StepFailedError(CANARY)):
                self.assertEqual(
                    lifecycle_native_failure.failure_codes(error), {}
                )

    def test_invalid_codes_and_private_fields_cannot_transfer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as child,
            tempfile.TemporaryDirectory() as outer,
        ):
            leaf = Path(child) / "muninn-native-diagnostics"
            leaf.mkdir()
            payload = {
                "phase": "upgrade_install",
                "status": "failed",
                "step_command_family": 15,
                "step_callsite": True,
                "step_exit_status": 4294967296,
                "argv": [CANARY],
                "stderr": CANARY,
                "env": {CANARY: CANARY},
            }
            (leaf / "muninn-lifecycle-proof.json").write_text(
                json.dumps(payload)
            )
            with mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": outer}
            ):
                transfer("lifecycle", Path(child))
                for key, value in (
                    ("step_command_family", -1),
                    ("step_callsite", 21),
                    ("step_exit_status", True),
                ):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        write("lifecycle", None, metrics={key: value})
            report = json.loads(
                (
                    Path(outer)
                    / "muninn-native-diagnostics"
                    / "muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(
                report, {"phase": "upgrade_install", "status": "failed"}
            )

    def test_successful_must_returns_the_original_result(self) -> None:
        result = subprocess.CompletedProcess(["git", "show"], 0, b"data", b"")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), mock.Mock(return_value=result), "success")
            self.assertIs(must(ctx, ["git", "show"]), result)
