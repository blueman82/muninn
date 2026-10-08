"""Lifecycle failure diagnostics retain fixed XML codes and source IDs."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import (
    lifecycle_native,
    lifecycle_native_linux,
    lifecycle_native_windows,
)


class LifecycleDiagnosticTests(unittest.TestCase):
    """External error and journal text stays out of CI diagnostics."""

    def test_registration_extracts_only_fixed_codes_and_positions(
        self,
    ) -> None:
        value = lifecycle_native_windows.registration_codes(
            b"ERROR: The task XML contains an unexpected node. "
            b"(1,12):Task: secret credential"
        )
        self.assertEqual(
            value,
            {
                "code": "task_xml_refused",
                "xml_line": 1,
                "xml_column": 12,
                "xml_element": "Task",
            },
        )
        self.assertNotIn("secret", json.dumps(value))
        unknown = lifecycle_native_windows.registration_codes(
            b"private transcript error"
        )
        self.assertEqual(unknown, {"code": "task_registration_unknown"})

    def test_journal_exports_ids_not_message_or_foreign_paths(self) -> None:
        source = {
            "MESSAGE": "private transcript and credential",
            "ERRNO": "5",
            "CODE_LINE": "123",
            "CODE_FILE": "src/core/main.c",
            "MESSAGE_ID": "a" * 32,
            "arbitrary": "secret",
        }
        self.assertEqual(
            lifecycle_native_linux.journal_codes(json.dumps(source).encode()),
            [
                {
                    "ERRNO": 5,
                    "CODE_LINE": 123,
                    "MESSAGE_ID": "a" * 32,
                    "source_id": "main.c",
                }
            ],
        )

    def test_xml_validation_exports_only_com_result_numbers(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            b'{"hresult":-2146233087,"inner_hresult":-2147216616,"message":"secret"}',
            b"private error",
        )
        with mock.patch.object(
            lifecycle_native_windows.subprocess, "run", return_value=result
        ):
            codes = lifecycle_native_windows.xml_validation_codes(
                Path("/synthetic/outer.xml")
            )
        self.assertEqual(
            codes, {"hresult": -2146233087, "inner_hresult": -2147216616}
        )


class DelegationTests(unittest.TestCase):
    """The isolated manager requires an ordinary user owned cgroup."""

    def test_current_owner_and_escape_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / "synthetic.service"
            group.mkdir()
            owner = group.stat().st_uid
            self.assertEqual(
                lifecycle_native_linux.cgroup_owner(
                    b"0::/synthetic.service\n", root, owner
                ),
                owner,
            )
            for record in (b"0::/../escape\n", b"1:cpu:/legacy\n"):
                with self.assertRaises(RuntimeError):
                    lifecycle_native_linux.cgroup_owner(record, root, owner)
            with self.assertRaises(PermissionError):
                lifecycle_native_linux.cgroup_owner(
                    b"0::/synthetic.service\n", root, owner + 1
                )

    def test_ci_service_forwards_diagnostic_root(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/platforms.yml"
        ).read_text()
        self.assertIn("--pipe --wait --collect", workflow)
        self.assertIn("MUNINN_NATIVE_SCRATCH=", workflow)
        self.assertNotIn("systemd-run --scope", workflow)
        self.assertIn("--property=SendSIGKILL=no", workflow)
        self.assertIn("--property=TimeoutStopSec=infinity", workflow)


class VendorUnitTests(unittest.TestCase):
    """Copy only the finite standard dependency chain into private CI state."""

    def test_missing_required_unit_has_no_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            destination = root / "vendor"
            with self.assertRaises(FileNotFoundError):
                lifecycle_native_linux.copy_vendor_units(source, destination)
            self.assertFalse(destination.exists())

    def test_copy_set_excludes_ambient_wants_and_keeps_private_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            names = (
                "basic.target",
                "sockets.target",
                "timers.target",
                "paths.target",
                "shutdown.target",
                "exit.target",
                "systemd-exit.service",
            )
            for name in names:
                (source / name).write_bytes(b"[Unit]\nDescription=Synthetic\n")
            ambient = source / "default.target.wants"
            ambient.mkdir()
            (ambient / "foreign.service").write_bytes(b"private credential")
            destination = root / "vendor"
            lifecycle_native_linux.copy_vendor_units(source, destination)
            self.assertEqual(
                {p.name for p in destination.iterdir()}, set(names)
            )
            for name in names:
                self.assertEqual(
                    (destination / name).read_bytes(),
                    (source / name).read_bytes(),
                )
                self.assertTrue(
                    lifecycle_native_linux.platform_io.is_private(
                        destination / name
                    )
                )

    def test_windows_vendor_fixture_checks_acl_not_posix_write_bits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "vendor.target"
            source.write_bytes(b"[Unit]\n")
            source.chmod(0o666)
            with (
                mock.patch.object(
                    lifecycle_native_linux.sys, "platform", "win32"
                ),
                mock.patch.object(
                    lifecycle_native_linux.platform_io,
                    "is_private",
                    return_value=True,
                ) as private,
            ):
                lifecycle_native_linux.ordinary_vendor(source)
            private.assert_called_once_with(source)
            with (
                mock.patch.object(
                    lifecycle_native_linux.sys, "platform", "win32"
                ),
                mock.patch.object(
                    lifecycle_native_linux.platform_io,
                    "is_private",
                    return_value=False,
                ),
                self.assertRaises(PermissionError),
            ):
                lifecycle_native_linux.ordinary_vendor(source)


class XmlEncodingProbeTests(unittest.TestCase):
    """Compare encoding declarations without registering any task."""

    def test_variant_result_codes_are_preserved(self) -> None:
        codes = {
            "hresult": -1,
            "inner_hresult": -1,
            "xml_utf8_hresult": -1,
            "xml_utf16_hresult": 0,
            "xml_omitted_hresult": 0,
        }
        result = subprocess.CompletedProcess(
            [], 0, json.dumps(codes).encode(), b""
        )
        with mock.patch.object(
            lifecycle_native_windows.subprocess, "run", return_value=result
        ) as run:
            self.assertEqual(
                lifecycle_native_windows.xml_validation_codes(
                    Path("synthetic.xml")
                ),
                codes,
            )
        argv = run.call_args.args[0]
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertIn(r"^<\?xml[^>]*>\s*", script)
        self.assertNotIn(r"^<\\?xml", script)

    def test_invalid_original_never_registers_task(self) -> None:
        with (
            mock.patch.object(
                lifecycle_native_windows,
                "xml_validation_codes",
                return_value={"hresult": -1},
            ),
            mock.patch.object(lifecycle_native_windows, "write"),
            mock.patch.object(
                lifecycle_native_windows.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ) as run,
            self.assertRaises(RuntimeError),
        ):
            lifecycle_native_windows._create_task(
                "synthetic", Path("synthetic.xml")
            )
        run.assert_not_called()


class ManagerCleanupTests(unittest.TestCase):
    """Cleanup failures preserve the original native lifecycle refusal."""

    def test_cleanup_error_preserves_primary_failure(self) -> None:
        process: mock.Mock = mock.Mock(pid=123)
        error = RuntimeError("synthetic cleanup refusal")
        with (
            mock.patch.object(
                lifecycle_native_linux, "stop_manager", side_effect=error
            ),
            mock.patch.object(lifecycle_native_linux, "write"),
            mock.patch("builtins.print"),
        ):
            lifecycle_native_linux.finish_manager(
                process, ValueError("original failure")
            )

    def test_cleanup_error_without_primary_is_still_failure(self) -> None:
        process: mock.Mock = mock.Mock(pid=123)
        error = RuntimeError("synthetic cleanup refusal")
        with (
            mock.patch.object(
                lifecycle_native_linux, "stop_manager", side_effect=error
            ),
            mock.patch.object(lifecycle_native_linux, "write"),
            mock.patch("builtins.print"),
            self.assertRaisesRegex(RuntimeError, "synthetic cleanup refusal"),
        ):
            lifecycle_native_linux.finish_manager(process, None)

    def test_startup_failure_restores_environment(self) -> None:
        before = dict(os.environ)
        with (
            mock.patch.object(lifecycle_native_linux.sys, "platform", "linux"),
            mock.patch.object(
                lifecycle_native_linux,
                "manager_environment",
                return_value={"XDG_RUNTIME_DIR": "/synthetic-runtime"},
            ),
            mock.patch.object(
                lifecycle_native_linux.shutil, "which", return_value="systemd"
            ),
            mock.patch.object(
                lifecycle_native_linux, "startup", side_effect=RuntimeError
            ),
            self.assertRaises(RuntimeError),
            lifecycle_native_linux.manager(Path("/synthetic")),
        ):
            self.fail("startup refusal cannot yield")
        self.assertTrue(
            dict(os.environ) == before, "startup changed caller environment"
        )


class NativeHarnessCleanupTests(unittest.TestCase):
    """A cleanup failure cannot publish a completed native acceptance proof."""

    def test_cleanup_refusal_preserves_failure_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "native"
            parent.mkdir()
            with (
                mock.patch.object(lifecycle_native.sys, "argv", ["native"]),
                mock.patch.object(lifecycle_native.sys, "platform", "linux"),
                mock.patch.object(
                    lifecycle_native.tempfile,
                    "mkdtemp",
                    return_value=str(parent),
                ),
                mock.patch.object(lifecycle_native, "refused_main"),
                mock.patch.object(
                    lifecycle_native, "exercise", return_value={}
                ),
                mock.patch.object(
                    lifecycle_native_linux,
                    "manager",
                    return_value=contextlib.nullcontext(),
                ),
                mock.patch(
                    "shutil.rmtree",
                    side_effect=PermissionError("owned cleanup refused"),
                ),
                mock.patch.object(lifecycle_native, "write") as write,
                mock.patch("builtins.print"),
                self.assertRaises(PermissionError),
            ):
                lifecycle_native.main()
            self.assertTrue(parent.exists())
            self.assertFalse(
                any(
                    call.kwargs.get("completed")
                    for call in write.call_args_list
                )
            )


class OrdinaryTokenDiagnosticTests(unittest.TestCase):
    """Only identity shape and privilege counters leave the synthetic child."""

    def test_identity_exports_shape_and_admin_count_only(self) -> None:
        value = lifecycle_native_windows.identity_codes(
            b'{"sid":"S-1-5-21-1-2-3-1001","elevated":true,"text":"private"}'
        )
        self.assertEqual(value, {"sid_valid": 1, "admin_member": 1})
        self.assertNotIn("S-1", json.dumps(value))
        self.assertEqual(
            lifecycle_native_windows.identity_codes(
                b'{"sid":"private transcript","elevated":false}'
            ),
            {"sid_valid": 0, "admin_member": 0},
        )

    def test_native_token_probe_refuses_other_platforms(self) -> None:
        with (
            mock.patch.object(
                lifecycle_native_windows.sys, "platform", "darwin"
            ),
            self.assertRaises(OSError),
        ):
            lifecycle_native_windows.token_codes()
