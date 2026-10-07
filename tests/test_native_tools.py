"""Windows development executables are found without bypassing gates."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.ingest_support import ROOT
from tools import check


class NativeToolTests(unittest.TestCase):
    """Prefer the checkout's Scripts executables to unrelated PATH tools."""

    def test_windows_scripts_executable_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool = root / ".venv" / "Scripts" / "ruff.exe"
            tool.parent.mkdir(parents=True)
            tool.touch()
            with (
                mock.patch.object(check, "ROOT", root),
                mock.patch.object(check, "main_checkout", return_value=root),
                mock.patch.object(check.shutil, "which", return_value=None),
            ):
                self.assertEqual(check.find_tool("ruff"), str(tool))

    def test_shell_sources_keep_lf_on_windows_checkout(self) -> None:
        done = subprocess.run(
            [
                "git",
                "check-attr",
                "eol",
                "--",
                "bin/muninn",
                "bin/muninn-install",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            done.stdout.splitlines(),
            ["bin/muninn: eol: lf", "bin/muninn-install: eol: lf"],
        )
