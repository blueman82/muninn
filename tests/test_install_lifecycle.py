"""Service definitions preserve transaction-safe stop and user ownership."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from install.context import Ctx, run_real
from install.lifecycle import linux_unit, task_xml
from muninn.obs_service import task_matches


class LifecycleDefinitionTests(unittest.TestCase):
    """Check the effective settings rather than a backend class hierarchy."""

    def test_linux_never_force_kills_a_writer(self) -> None:
        ctx = Ctx(Path("/synthetic/home"), run_real, "test", platform="linux")
        unit = linux_unit(
            ctx, Path("/private/python"), Path("/private/release")
        )
        self.assertIn("TimeoutStopSec=infinity", unit)
        self.assertIn("SendSIGKILL=no", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_windows_action_has_no_elevation_password_or_cmd_layer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "test", platform="win32")
            xml = task_xml(
                ctx,
                "S-1-5-21-123",
                Path("C:/private/python.exe"),
                Path("C:/private/release"),
            )
        root = ET.fromstring(xml)
        namespace = {
            "t": "http://schemas.microsoft.com/windows/2004/02/mit/task"
        }
        self.assertEqual(
            root.findtext(
                "t:Principals/t:Principal/t:LogonType", namespaces=namespace
            ),
            "InteractiveToken",
        )
        self.assertEqual(
            root.findtext(
                "t:Principals/t:Principal/t:RunLevel", namespaces=namespace
            ),
            "LeastPrivilege",
        )
        self.assertEqual(
            root.findtext(
                "t:Settings/t:AllowHardTerminate", namespaces=namespace
            ),
            "false",
        )
        self.assertEqual(
            root.findtext(
                "t:Settings/t:ExecutionTimeLimit", namespaces=namespace
            ),
            "PT0S",
        )
        command = root.findtext(
            "t:Actions/t:Exec/t:Command", namespaces=namespace
        )
        arguments = root.findtext(
            "t:Actions/t:Exec/t:Arguments", namespaces=namespace
        )
        assert command is not None and arguments is not None
        self.assertTrue(command.endswith("powershell.exe"))
        self.assertIn("-EncodedCommand", arguments)
        self.assertNotIn("Password", xml.decode())

    def test_task_definition_changes_refuse_ownership(self) -> None:
        ctx = Ctx(Path("/synthetic/home"), run_real, "test", platform="win32")
        xml = task_xml(ctx, "S-1-5-21-123", Path("/python"), Path("/release"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for path in (
            "t:Settings/t:RestartOnFailure/t:Count",
            "t:Triggers/t:LogonTrigger/t:UserId",
            "t:Triggers/t:LogonTrigger/t:Enabled",
            "t:Settings/t:Enabled",
            "t:Settings/t:DisallowStartIfOnBatteries",
            "t:Settings/t:StartWhenAvailable",
        ):
            with self.subTest(path=path):
                expected, actual = ET.fromstring(xml), ET.fromstring(xml)
                field = actual.find(path, ns)
                assert field is not None
                field.text = "unexpected"
                self.assertFalse(task_matches(expected, actual))
