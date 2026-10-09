"""Native lifecycle diagnostics retain only fixed doctor boundary codes."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import lifecycle_native_doctor
from tests.lifecycle_native_doctor import codes


class NativeDoctorCodesTests(unittest.TestCase):
    """Keep unknown and refusal diagnostics bounded without native claims."""

    def test_fixed_check_and_native_query_fields_only(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "checks": [
                        {
                            "check": "managed_service",
                            "ok": None,
                            "detail": "service_state_unknown",
                        },
                        {"check": "secret", "detail": "credential"},
                    ]
                }
            ).encode(),
            b"credential",
        )
        with (
            patch(
                "tests.lifecycle_native_doctor.obs_linux_service.query_unit",
                return_value={
                    "LoadState": "loaded",
                    "MainPID": "44",
                    "ActiveState": "active",
                    "FragmentPath": "/private/unit",
                    "DropInPaths": "",
                },
            ),
            patch(
                "tests.lifecycle_native_doctor.obs_status.read_status",
                return_value={"pid": 44},
            ),
            patch(
                "tests.lifecycle_native_doctor.obs_linux_service.inspect",
                side_effect=ValueError("process_command_invalid"),
            ),
        ):
            report = codes(Path("/synthetic"), {"HOME": "/synthetic"}, result)
        self.assertEqual(report["doctor_code"], "service_state_unknown")
        self.assertEqual(report["query_key_count"], 5)
        self.assertEqual(report["dropins_empty"], 1)
        self.assertEqual(report["pid_matches"], 1)
        self.assertEqual(report["inspect_code"], "process_command_invalid")
        self.assertNotIn("credential", json.dumps(report))
        self.assertNotIn("/private/unit", json.dumps(report))

    def test_arbitrary_details_and_errors_remain_unknown(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            b'{"checks":[{"check":"managed_service","ok":false,'
            b'"detail":"credential transcript"}]}',
            b"",
        )
        with (
            patch(
                "tests.lifecycle_native_doctor.obs_linux_service.query_unit",
                side_effect=OSError(13, "credential transcript"),
            ),
            patch(
                "tests.lifecycle_native_doctor.obs_linux_service.inspect",
                side_effect=ValueError("credential transcript"),
            ),
        ):
            report = codes(Path("/synthetic"), {}, result)
        self.assertEqual(report["doctor_code"], "unknown")
        self.assertEqual(report["query_errno"], 13)
        self.assertEqual(report["inspect_code"], "unknown")
        self.assertNotIn("credential", json.dumps(report))


class InterpreterMetadataTests(unittest.TestCase):
    """Private diagnostic metadata never exports executable or user paths."""

    def test_regular_and_write_masks_are_numeric_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            executable = home / "python"
            executable.write_bytes(b"not executed")
            executable.chmod(0o775)
            metadata = executable.stat()
            with patch.object(Path, "readlink", return_value=executable):
                report = lifecycle_native_doctor.interpreter_codes(
                    home, metadata.st_uid
                )
        self.assertEqual(
            report["interpreter_regular"], int(stat.S_ISREG(metadata.st_mode))
        )
        self.assertEqual(
            report["interpreter_write_mask"],
            stat.S_IMODE(metadata.st_mode) & 0o022,
        )
        self.assertEqual(report["interpreter_owner_trusted"], 1)
        self.assertTrue(all(type(value) is int for value in report.values()))
        self.assertNotIn(temporary, json.dumps(report))
