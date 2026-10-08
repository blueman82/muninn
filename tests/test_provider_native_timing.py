"""Timing diagnostics inspect only bounded native executable machine codes."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.context import Ctx, run_real
from muninn import platform_windows
from tests.provider_native import (
    baselines,
    emitter_baseline,
    pe_machine,
    prove_hooks,
)


class MachineCodesTest(unittest.TestCase):
    """Executable bytes stay private; malformed headers refuse."""

    def test_reports_bounded_pe_machine_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.exe"
            header = bytearray(64)
            header[:2] = b"MZ"
            header[60:64] = (64).to_bytes(4, "little")
            path.write_bytes(bytes(header) + b"PE\x00\x00\x64\xaa")
            self.assertEqual(pe_machine(path), 0xAA64)
            path.write_bytes(bytes(header) + b"PE\x00\x00\x64\x86")
            self.assertEqual(pe_machine(path), 0x8664)
            for raw in (
                b"private binary",
                bytes(header),
                b"MZ" + b"\xff" * 62,
            ):
                path.write_bytes(raw)
                with self.assertRaises(OSError):
                    pe_machine(path)

    def test_baselines_use_three_fresh_native_processes(self) -> None:
        with (
            mock.patch("tests.provider_native.sys.platform", "win32"),
            mock.patch.object(
                platform_windows, "assert_executable", create=True
            ),
            mock.patch(
                "tests.provider_native.pe_machine", return_value=0xAA64
            ),
            mock.patch(
                "tests.provider_native.time.monotonic",
                side_effect=[0, 1, 2, 3, 4, 5],
            ),
            mock.patch(
                "tests.provider_native.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ) as run,
        ):
            result = baselines({"HOME": "synthetic"})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["python_pe_machine"], 0xAA64)
        self.assertEqual(result["powershell_pe_machine"], 0xAA64)
        for name in ("python", "powershell", "addtype"):
            self.assertEqual(result[f"baseline_{name}_ms"], 1000)
            self.assertEqual(result[f"baseline_{name}_returncode"], 0)

    def test_non_windows_baselines_do_not_run_processes(self) -> None:
        with (
            mock.patch("tests.provider_native.sys.platform", "darwin"),
            mock.patch("tests.provider_native.subprocess.run") as run,
        ):
            self.assertEqual(baselines({}), {})
            run.assert_not_called()

    def test_optional_diagnostic_write_does_not_mask_hook_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "synthetic", platform="darwin")
            with (
                mock.patch("tests.provider_native.configedit.atomic_write"),
                mock.patch(
                    "tests.provider_native.hook_invocations",
                    return_value=[["synthetic"]],
                ),
                mock.patch(
                    "tests.provider_native.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, b"{}", b"synthetic stderr"
                    ),
                ),
                mock.patch("tests.provider_native.baselines", return_value={}),
                mock.patch(
                    "tests.provider_native.write",
                    side_effect=[
                        None,
                        None,
                        None,
                        PermissionError("optional metrics refused"),
                    ],
                ),
                self.assertRaisesRegex(AssertionError, "unexpected stderr"),
            ):
                prove_hooks(ctx, {})

    def test_post_failure_emitter_isolation_uses_actual_source_functions(
        self,
    ) -> None:
        with (
            mock.patch("tests.provider_native.sys.platform", "win32"),
            mock.patch.object(
                platform_windows, "assert_executable", create=True
            ),
            mock.patch(
                "tests.provider_native.time.monotonic", side_effect=[0, 1]
            ),
            mock.patch(
                "tests.provider_native.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ) as run,
        ):
            result = emitter_baseline({"HOME": "synthetic"})
        command = run.call_args.args[0][-1]
        self.assertIn("DefinePInvokeMethod", command)
        self.assertIn("[void](Initialize-Native)", command)
        self.assertNotIn("Open-Native", command)
        self.assertNotIn("Add-Type", command)
        self.assertEqual(
            result,
            {
                "baseline_emit_ms": 1000,
                "baseline_emit_returncode": 0,
                "baseline_emit_error": 0,
            },
        )

    def test_emitter_baseline_never_starts_before_actual_failed_route(
        self,
    ) -> None:
        events: list[str] = []

        def failed_route(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            events.append("route")
            return subprocess.CompletedProcess(
                [], 0, b"{}", b"synthetic stderr"
            )

        def isolation(env: dict[str, str]) -> dict[str, int]:
            events.append("emitter")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "synthetic", platform="darwin")
            with (
                mock.patch("tests.provider_native.configedit.atomic_write"),
                mock.patch(
                    "tests.provider_native.hook_invocations",
                    return_value=[["synthetic"]],
                ),
                mock.patch(
                    "tests.provider_native.subprocess.run",
                    side_effect=failed_route,
                ),
                mock.patch(
                    "tests.provider_native.emitter_baseline",
                    side_effect=isolation,
                ),
                mock.patch("tests.provider_native.baselines", return_value={}),
                mock.patch("tests.provider_native.write"),
                self.assertRaisesRegex(AssertionError, "unexpected stderr"),
            ):
                prove_hooks(ctx, {})
        self.assertEqual(events, ["route", "emitter"])

    def test_non_windows_emitter_isolation_does_not_start_a_process(
        self,
    ) -> None:
        with (
            mock.patch("tests.provider_native.sys.platform", "darwin"),
            mock.patch("tests.provider_native.subprocess.run") as run,
        ):
            self.assertEqual(emitter_baseline({}), {})
            run.assert_not_called()
