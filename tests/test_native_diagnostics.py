"""Early native proof artifacts retain only explicit codes and counts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.config_windows import SecurityMismatchError, security_parts
from tests.native_diagnostics import transfer, write
from tests.test_config_windows_metadata import descriptor


class NativeDiagnosticsTest(unittest.TestCase):
    """Failure metadata cannot disclose exception messages or foreign paths."""

    def test_failure_preserves_phase_and_safe_frame_without_message(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            write("provider", "fixture_ingest", metrics={"route": 2})
            try:
                raise PermissionError(5, "private transcript and credential")
            except PermissionError as error:
                write("provider", None, error=error)
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-provider-proof.json"
            )
            text = path.read_text()
            data = json.loads(text)
            self.assertEqual(data["phase"], "fixture_ingest")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["route"], 2)
            self.assertEqual(data["error_type"], "PermissionError")
            self.assertEqual(data["errno"], 5)
            self.assertEqual(
                data["error_module"], "tests/test_native_diagnostics.py"
            )
            self.assertIsInstance(data["error_line"], int)
            self.assertNotIn("private transcript", text)
            self.assertNotIn(tmp, text)
            write("provider", None, error=RuntimeError("outer failure"))
            self.assertEqual(path.read_text(), text)

    def test_unknown_phase_or_field_is_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            with self.assertRaises(ValueError):
                write("provider", "arbitrary transcript")
            with self.assertRaises(ValueError):
                write("provider", "routes", metrics={"private_field": 1})
            self.assertFalse(
                (
                    Path(tmp)
                    / "muninn-native-diagnostics"
                    / "muninn-provider-proof.json"
                ).exists()
            )

    def test_success_replaces_previous_failure_with_counts(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            write(
                "lifecycle", "fresh_install", error=RuntimeError("not logged")
            )
            write(
                "lifecycle",
                "complete",
                metrics={"hooks_count": 4},
                completed=True,
            )
            data = json.loads(
                (
                    Path(tmp)
                    / "muninn-native-diagnostics"
                    / "muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(
                data,
                {"phase": "complete", "status": "passed", "hooks_count": 4},
            )

    def test_transfer_drops_untrusted_fields_and_values(self) -> None:
        with (
            tempfile.TemporaryDirectory() as child,
            tempfile.TemporaryDirectory() as outer,
        ):
            source = Path(child) / "muninn-native-diagnostics"
            source.mkdir(mode=0o700)
            payload = {
                "phase": "fresh_stop",
                "status": "failed",
                "winerror": 5,
                "error_module": "/private/transcript.py",
                "message": "secret",
                "error_type": ["unsafe"],
                "elapsed_ms": "private",
            }
            (source / "muninn-lifecycle-proof.json").write_text(
                json.dumps(payload)
            )
            with mock.patch.dict("os.environ", {"RUNNER_TEMP": outer}):
                transfer("lifecycle", Path(child))
            result = json.loads(
                (
                    Path(outer)
                    / "muninn-native-diagnostics"
                    / "muninn-lifecycle-proof.json"
                ).read_text()
            )
            self.assertEqual(
                result,
                {"phase": "fresh_stop", "status": "failed", "winerror": 5},
            )

    def test_outer_task_phases_accept_only_numeric_hresult(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            write(
                "lifecycle",
                "outer_task_create",
                metrics={"hresult": -2147216615, "inner_hresult": -2147024809},
            )
            path = (
                Path(tmp)
                / "muninn-native-diagnostics"
                / "muninn-lifecycle-proof.json"
            )
            data = json.loads(path.read_text())
            self.assertEqual(data["phase"], "outer_task_create")
            self.assertEqual(data["hresult"], -2147216615)
            self.assertEqual(data["inner_hresult"], -2147024809)
            write("lifecycle", "outer_task_run")
            self.assertEqual(
                json.loads(path.read_text())["phase"], "outer_task_run"
            )
            with self.assertRaises(ValueError):
                write("lifecycle", "outer_task_create", metrics={"raw_xml": 1})

    def test_explicit_private_scratch_ignores_broad_runner_directory(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as private,
            tempfile.TemporaryDirectory() as broad,
        ):
            with mock.patch.dict(
                "os.environ",
                {"MUNINN_NATIVE_SCRATCH": private, "RUNNER_TEMP": broad},
            ):
                write("provider", "fixture_home")
            leaf = "muninn-native-diagnostics"
            self.assertTrue(
                (Path(private) / leaf / "muninn-provider-proof.json").is_file()
            )
            self.assertFalse((Path(broad) / leaf).exists())

    def test_native_security_mismatch_exports_only_numeric_codes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            error = SecurityMismatchError(
                "control",
                security_parts(descriptor()),
                security_parts(descriptor(flags=0x8004)),
            )
            write("provider", "codex_render", error=error)
            data = json.loads(
                (
                    Path(tmp)
                    / "muninn-native-diagnostics"
                    / "muninn-provider-proof.json"
                ).read_text()
            )
            self.assertEqual(data["control_before"], 0x8404)
            self.assertEqual(data["control_after"], 0x8004)
            self.assertEqual(
                [
                    data[key]
                    for key in ("owner_equal", "group_equal", "dacl_equal")
                ],
                [1, 1, 1],
            )
            self.assertNotIn("config security differs", json.dumps(data))

    def test_xml_declaration_probe_exports_numeric_hresult_only(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"RUNNER_TEMP": tmp}),
        ):
            codes = {
                "xml_utf8_hresult": -2147216615,
                "xml_utf16_hresult": 0,
                "xml_omitted_hresult": 0,
            }
            write("lifecycle", "outer_task_create", metrics=codes)
            data = json.loads(
                (
                    Path(tmp)
                    / "muninn-native-diagnostics"
                    / "muninn-lifecycle-proof.json"
                ).read_text()
            )
            for key, value in codes.items():
                self.assertEqual(data[key], value)
