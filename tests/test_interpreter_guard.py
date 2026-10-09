"""Interpreter fields reject script candidates before version execution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from muninn import platform_windows
from tests.ingest_support import ROOT


class InterpreterGuardContractTests(unittest.TestCase):
    """Keep the source guard on the shared interpreter-selection boundary."""

    def test_cmd_facade_pins_system_powershell(self) -> None:
        source = (ROOT / "bin/muninn.cmd").read_text(encoding="utf-8")
        self.assertIn(
            '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"',
            source,
        )
        self.assertNotIn("\npowershell.exe ", source)

    def test_powershell_field_checks_exe_before_probe(self) -> None:
        source = (ROOT / "bin/muninn.ps1").read_text(encoding="utf-8")
        field = source.split("function Assert-InterpreterField", 1)[1].split(
            "function Assert-Executable", 1
        )[0]
        self.assertIn("[IO.Path]::GetExtension($full) -ine '.exe'", field)
        self.assertIn("Assert-InterpreterField $env:MUNINN_PYTHON", source)
        self.assertIn("Assert-InterpreterField $record.python", source)
        self.assertIn("Assert-InterpreterField $command.Path", source)


if sys.platform == "win32":

    class NativeInterpreterGuardTests(unittest.TestCase):
        """Native interpreter fields never accept batch or scripts."""

        def test_scripts_refuse_even_when_missing(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                for name in (
                    "missing.cmd",
                    "missing.CMD",
                    "missing.ps1",
                    "missing.py",
                ):
                    with self.subTest(name=name), self.assertRaises(OSError):
                        platform_windows.assert_interpreter_field(
                            Path(temporary) / name
                        )
                platform_windows.assert_interpreter_field(
                    Path(temporary) / "missing.EXE"
                )
