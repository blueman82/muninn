"""Timing diagnostics inspect only bounded native executable machine codes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install.context import Ctx, run_real
from muninn import knowledge_read, platform_windows, store
from muninn.knowledge_models import Scope
from tests.provider_native import (
    baselines,
    emitter_baseline,
    fixture,
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
                    "tests.provider_native._context_calls",
                    return_value=[
                        ["synthetic", "session-start", "--provider", "claude"]
                    ],
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

        def stages(
            ctx: Ctx,
            env: dict[str, str],
            argv: str | list[str],
            payload: bytes,
        ) -> dict[str, int]:
            events.append("stages")
            return {}

        def isolation(env: dict[str, str]) -> dict[str, int]:
            events.append("emitter")
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "synthetic", platform="darwin")
            with (
                mock.patch("tests.provider_native.configedit.atomic_write"),
                mock.patch(
                    "tests.provider_native._context_calls",
                    return_value=[
                        ["synthetic", "session-start", "--provider", "claude"]
                    ],
                ),
                mock.patch(
                    "tests.provider_native.subprocess.run",
                    side_effect=failed_route,
                ),
                mock.patch(
                    "tests.provider_native.launcher_stages", side_effect=stages
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
        self.assertEqual(events, ["route", "stages", "emitter"])

    def test_non_windows_emitter_isolation_does_not_start_a_process(
        self,
    ) -> None:
        with (
            mock.patch("tests.provider_native.sys.platform", "darwin"),
            mock.patch("tests.provider_native.subprocess.run") as run,
        ):
            self.assertEqual(emitter_baseline({}), {})
            run.assert_not_called()


class NativeFixtureTests(unittest.TestCase):
    """Keep fixture content checked without claiming native host acceptance."""

    def test_rendered_context_routes_accept_native_lifecycle_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp).resolve() / "space café 雪 owner's $ % home",
                run_real,
                "synthetic",
            )
            env = fixture(ctx, lambda phase: None)
            with mock.patch(
                "install.provider_paths.installed", return_value=False
            ):
                results = prove_hooks(ctx, env)
            self.assertEqual(len(results), 2)
            self.assertEqual([item["route"] for item in results], [0, 1])

    def test_fixture_retains_real_normalized_support_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            ctx = Ctx(home, run_real, "synthetic", platform=sys.platform)
            phases: list[str] = []
            fixture(ctx, phases.append)
            with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
                views = knowledge_read.search_knowledge(
                    conn, "nativealpha", Scope(project_id=str(home), task=None)
                )
                self.assertEqual(len(views), 1)
                self.assertIsNotNone(views[0].body)
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM review_receipt"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM knowledge_support"
                    ).fetchone()[0],
                    1,
                )
            self.assertIn("fixture_knowledge", phases)
