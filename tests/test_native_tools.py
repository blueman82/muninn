"""Windows development executables are found without bypassing gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
