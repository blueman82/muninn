"""Actual Windows installer dispatch through the existing trusted launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from tests.ingest_support import ROOT

if sys.platform == "win32":
    from tests.test_native_launcher import NativeLauncherTests

    class NativeInstallEntryTests(unittest.TestCase):
        """Reuse the native receiver fixture without inheriting its tests."""

        def setUp(self) -> None:
            self.fixture = NativeLauncherTests()
            self.fixture.setUp()
            self.addCleanup(self.fixture.doCleanups)
            package = self.fixture.root / "install"
            package.mkdir()
            (package / "__init__.py").touch()
            (package / "entry.py").write_text(
                '"""Synthetic native installer receiver."""\n'
                "import json,sys\n"
                "def main(args: list[str]) -> int:\n"
                '    """Preserve arguments and return a distinct status."""\n'
                "    print(json.dumps(args))\n"
                "    return 23\n",
                encoding="utf-8",
            )
            for name in (
                "muninn-install.cmd",
                "muninn-install.ps1",
                "muninn-uninstall.cmd",
                "muninn-uninstall.ps1",
            ):
                shutil.copyfile(ROOT / "bin" / name, self.fixture.bin / name)

        def test_first_marker_dispatches_and_preserves_args(self) -> None:
            args = ["--muninn-installer-entry", "--check", "café 雪 &"]
            done = self.fixture.run_cmd(args)
            self.assertEqual(done.returncode, 23, done.stderr)
            self.assertEqual(json.loads(done.stdout), args[1:])

        def test_later_marker_remains_an_ordinary_argument(self) -> None:
            args = ["stats", "--muninn-installer-entry"]
            done = self.fixture.run_cmd(args)
            self.assertEqual(done.returncode, 17, done.stderr)
            self.assertEqual(json.loads(done.stdout), args)

        def test_present_unsafe_python_still_refuses_installer(self) -> None:
            self.fixture.env["MUNINN_PYTHON"] = str(
                self.fixture.bin / "muninn.cmd"
            )
            done = self.fixture.run_cmd(["--muninn-installer-entry", "--help"])
            self.assertEqual(done.returncode, 127, done.stderr)
            self.assertEqual(done.stdout, "")

        def run_wrapper(
            self, name: str, args: list[str]
        ) -> subprocess.CompletedProcess[str]:
            """Execute the real dedicated source wrapper without an alias."""
            path = self.fixture.bin / name
            if name.endswith(".cmd"):
                command = subprocess.list2cmdline([str(path), *args])
                shell = subprocess.list2cmdline(
                    [os.environ.get("COMSPEC", "cmd.exe")]
                )
                argv: list[str] | str = shell + ' /d /s /c "' + command + '"'
            else:
                argv = [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(path),
                    *args,
                ]
            return subprocess.run(
                argv,
                env=self.fixture.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
            )

        def test_dedicated_install_wrappers_preserve_args_and_exit(
            self,
        ) -> None:
            args = ["--check", "café 雪 owner's", 'embedded "quote"', "& |"]
            for name in ("muninn-install.cmd", "muninn-install.ps1"):
                with self.subTest(name=name):
                    done = self.run_wrapper(name, args)
                    self.assertEqual(done.returncode, 23, done.stderr)
                    self.assertEqual(json.loads(done.stdout), args)

        def test_dedicated_uninstall_wrappers_forward_dry_run(self) -> None:
            for name in ("muninn-uninstall.cmd", "muninn-uninstall.ps1"):
                with self.subTest(name=name):
                    done = self.run_wrapper(name, ["--dry-run"])
                    self.assertEqual(done.returncode, 23, done.stderr)
                    self.assertEqual(
                        json.loads(done.stdout), ["--uninstall", "--dry-run"]
                    )
