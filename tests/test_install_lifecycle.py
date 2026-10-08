"""Service definitions preserve transaction-safe stop and user ownership."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

from install import installer
from install.context import Ctx, run_real
from install.lifecycle import _identity, linux_unit, task_xml
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
        self.assertNotIn("Password", xml.decode("utf-16"))

    def test_task_xml_has_consistent_utf16_declaration_and_bom(self) -> None:
        ctx = Ctx(Path("/synthetic/home"), run_real, "test", platform="win32")
        xml = task_xml(ctx, "S-1-5-21-123", Path("/python"), Path("/release"))
        self.assertIn(xml[:2], (b"\xff\xfe", b"\xfe\xff"))
        self.assertIn("encoding='utf-16'", xml.decode("utf-16"))
        self.assertEqual(ET.fromstring(xml).tag.rsplit("}", 1)[-1], "Task")

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

    def test_actual_current_sid_supports_local_and_azure_forms(self) -> None:
        for sid in ("S-1-5-21-1-2-3-1001", "S-1-12-1-1-2-3-4"):
            with self.subTest(sid=sid):

                def run(
                    argv: Sequence[str | Path],
                    env: Mapping[str, str] | None = None,
                    input: bytes | None = None,
                    *,
                    current_sid: str = sid,
                ) -> subprocess.CompletedProcess[bytes]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(
                            {"sid": current_sid, "elevated": False}
                        ).encode(),
                    )

                ctx = Ctx(
                    Path("/synthetic/home"), run, "sid", platform="win32"
                )
                self.assertEqual(_identity(ctx), sid)
                self.assertTrue(ctx.target.startswith("Muninn-"))

    def test_successful_install_finishes_with_service_running(self) -> None:
        fresh = [step.__name__ for step in installer.FRESH_STEPS]
        upgrade = [step.__name__ for step in installer.UPGRADE_STEPS]
        self.assertNotIn("quiesce", fresh)
        self.assertEqual(upgrade.count("quiesce"), 1)
        self.assertLess(
            upgrade.index("quiesce"), upgrade.index("snapshot_store")
        )
        self.assertEqual(fresh[-1], "prune")
        self.assertEqual(upgrade[-1], "prune")
