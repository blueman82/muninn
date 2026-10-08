"""Lifecycle failure diagnostics retain fixed XML codes and source IDs."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import lifecycle_native_linux, lifecycle_native_windows


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
