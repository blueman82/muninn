"""Lifecycle durations survive phase changes, child transfer and failures."""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from install.context import Ctx, run_real
from tests import lifecycle_native, lifecycle_native_windows
from tests.native_diagnostic_metrics import TIMING_PHASES, measure
from tests.native_diagnostics import transfer, write


class LifecycleTimingTests(unittest.TestCase):
    """Measure inclusive phase time without exporting private context."""

    def test_success_and_failure_keep_every_elapsed_phase(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": temporary}
            ),
            mock.patch(
                "tests.native_diagnostic_metrics.time.monotonic",
                side_effect=[1, 3, 4, 9],
            ),
        ):
            metrics: dict[str, int] = {}
            with measure("fresh_install", metrics):
                write("lifecycle", "fresh_install")
            write("lifecycle", "fresh_stop", metrics=metrics)
            error = AssertionError("private path and SID")
            try:
                with measure("fresh_stop", metrics):
                    raise error
            except AssertionError:
                write("lifecycle", None, error=error, metrics=metrics)
            write("lifecycle", None, metrics={"cleanup_ms": 7})
            path = (
                Path(temporary)
                / "muninn-native-diagnostics/muninn-lifecycle-proof.json"
            )
            data = json.loads(path.read_text())
            self.assertEqual(data["fresh_install_ms"], 2000)
            self.assertEqual(data["fresh_stop_ms"], 5000)
            self.assertEqual(data["cleanup_ms"], 7)
            self.assertEqual(data["phase"], "fresh_stop")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error_type"], "AssertionError")
            self.assertNotIn("private", path.read_text())

    def test_child_transfer_and_completion_keep_parent_and_child_times(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as parent,
            tempfile.TemporaryDirectory() as child,
        ):
            with mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": child}
            ):
                write(
                    "lifecycle",
                    "upgrade_stop",
                    metrics={"fresh_install_ms": 800, "upgrade_stop_ms": 200},
                )
            with mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": parent}
            ):
                write(
                    "lifecycle",
                    "outer_task_run",
                    metrics={"outer_setup_ms": 300},
                )
                transfer("lifecycle", Path(child))
                write("lifecycle", "complete", completed=True)
            data = json.loads(
                (
                    Path(parent)
                    / "muninn-native-diagnostics/muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(
                data,
                {
                    "phase": "complete",
                    "status": "passed",
                    "outer_setup_ms": 300,
                    "fresh_install_ms": 800,
                    "upgrade_stop_ms": 200,
                },
            )

    def test_timing_fields_and_phase_labels_are_bounded_and_fixed(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": temporary}
            ),
        ):
            for phase in TIMING_PHASES:
                write("lifecycle", phase, metrics={phase + "_ms": 4_500_000})
                for value in (-1, 4_500_001, True):
                    with self.assertRaises(ValueError):
                        write(
                            "lifecycle", phase, metrics={phase + "_ms": value}
                        )
            metrics: dict[str, int] = {}
            with (
                self.assertRaises(ValueError),
                measure("private_path", metrics),
            ):
                self.fail("untrusted phase entered")
            self.assertEqual(metrics, {})
            with self.assertRaises(ValueError):
                write("lifecycle", None, metrics={"command_ms": 1})

    def test_real_harness_times_include_observations_and_failed_assertions(
        self,
    ) -> None:
        self._exercise_case(fail=False)
        self._exercise_case(fail=True)

    def _exercise_case(self, *, fail: bool) -> None:
        """Run the actual harness with deterministic external operations."""
        with (
            tempfile.TemporaryDirectory() as temporary,
            contextlib.ExitStack() as patches,
        ):
            clock = [0.0]
            jobs = iter(({"pid": 10}, None, {"pid": 20}))
            statuses = iter(
                (
                    {"pid": 99 if fail else 10, "stop_generation": "first"},
                    {"pid": 20, "stop_generation": "second"},
                )
            )

            def install(ctx: Ctx, root: Path, sha: str) -> None:
                clock[0] += 0.1

            def job(ctx: Ctx) -> dict[str, int] | None:
                clock[0] += 0.5
                return next(jobs)

            def status(data: Path) -> dict[str, int | str]:
                clock[0] += 0.25
                return next(statuses)

            def key(data: Path, *, create: bool = True) -> bytes:
                clock[0] += 0.125
                return b"synthetic-key"

            def stop(ctx: Ctx) -> None:
                clock[0] += 0.2

            patches.enter_context(
                mock.patch.dict(
                    "os.environ", {"MUNINN_NATIVE_SCRATCH": temporary}
                )
            )
            patches.enter_context(
                mock.patch(
                    "tests.native_diagnostic_metrics.time.monotonic",
                    side_effect=lambda: clock[0],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    lifecycle_native,
                    "sys",
                    types.SimpleNamespace(platform="win32"),
                )
            )
            operations = (
                (lifecycle_native.lifecycle_native_entry, "install", install),
                (lifecycle_native.lifecycle, "job", job),
                (lifecycle_native.lifecycle, "stop", stop),
                (lifecycle_native.obs_status, "read_status", status),
                (lifecycle_native.tombstone_key, "load_key", key),
            )
            for target, name, operation in operations:
                patches.enter_context(
                    mock.patch.object(target, name, side_effect=operation)
                )
            for target, name in (
                (lifecycle_native, "cleanup_backend"),
                (lifecycle_native, "assert_service"),
                (lifecycle_native.lifecycle_native_writer, "busy_stop"),
                (lifecycle_native.lifecycle_native_writer, "crash_restart"),
                (lifecycle_native.lifecycle_native_rollback, "rollback"),
                (
                    lifecycle_native.lifecycle_native_entry,
                    "maintenance_checks",
                ),
            ):
                patches.enter_context(mock.patch.object(target, name))
            if fail:
                with self.assertRaises(AssertionError):
                    lifecycle_native.exercise(Path(temporary))
            else:
                lifecycle_native.exercise(Path(temporary))
            data = json.loads(
                (
                    Path(temporary)
                    / "muninn-native-diagnostics/muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(data["fresh_install_ms"], 850 if fail else 975)
            if not fail:
                self.assertEqual(data["fresh_stop_ms"], 700)
                self.assertEqual(data["upgrade_install_ms"], 975)
                self.assertEqual(data["upgrade_stop_ms"], 200)

    def test_outer_timeout_keeps_child_failure_codes_and_all_parent_timings(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as parent,
            tempfile.TemporaryDirectory() as child,
        ):
            outer = Path(parent)
            inner = Path(child)
            with mock.patch.dict(
                "os.environ", {"MUNINN_NATIVE_SCRATCH": child}
            ):
                write(
                    "lifecycle",
                    "fresh_stop",
                    error=AssertionError("private"),
                    metrics={"fresh_install_ms": 12},
                )
            with (
                mock.patch.dict(
                    "os.environ", {"MUNINN_NATIVE_SCRATCH": parent}
                ),
                mock.patch.object(
                    lifecycle_native_windows,
                    "_outer_task",
                    return_value=(
                        Ctx(inner, run_real, "outer"),
                        "synthetic",
                        inner / "outer.xml",
                    ),
                ),
                mock.patch.object(lifecycle_native_windows, "_create_task"),
                mock.patch.object(
                    lifecycle_native_windows.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch(
                    "tests.native_diagnostic_metrics.time.monotonic",
                    side_effect=[0, 1, 2, 3, 4, 5, 6, 7, 1808, 1809],
                ),
                self.assertRaisesRegex(
                    RuntimeError, "ordinary child timed out"
                ),
            ):
                lifecycle_native_windows.run_child(inner, outer)
            data = json.loads(
                (
                    outer
                    / "muninn-native-diagnostics/muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(data["phase"], "fresh_stop")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error_type"], "AssertionError")
            self.assertEqual(data["fresh_install_ms"], 12)
            self.assertEqual(data["outer_setup_ms"], 1000)
            self.assertEqual(data["outer_task_create_ms"], 1000)
            self.assertEqual(data["outer_task_run_ms"], 1000)
            self.assertEqual(data["outer_task_wait_ms"], 1_803_000)
