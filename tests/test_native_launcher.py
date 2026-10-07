"""Real cmd, PowerShell and Python forwarding in isolated Windows checkouts."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from muninn import platform_io, poller_stop, store
from tests.ingest_support import ROOT
from tests.test_bootstrap_guards import compile_marker, diagnose_bootstrap

if sys.platform == "win32":

    class NativeLauncherTests(unittest.TestCase):
        """Run the actual native wrappers, including quoting and exit codes."""

        def setUp(self) -> None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.root = Path(temp.name) / "space café 雪 owner's checkout"
            self.bin = self.root / "bin"
            self.bin.mkdir(parents=True)
            for name in ("muninn.cmd", "muninn.ps1"):
                shutil.copyfile(ROOT / "bin" / name, self.bin / name)
            diagnose_bootstrap(self.bin)
            package = self.root / "muninn"
            package.mkdir()
            (package / "__init__.py").touch()
            (package / "cli.py").write_text(
                '"""Synthetic argument receiver."""\n'
                "import json\n"
                "def main(args: list[str]) -> int:\n"
                '    """Print arguments and return a distinctive status."""\n'
                "    print(json.dumps(args))\n"
                "    return 17\n",
                encoding="utf-8",
            )
            self.env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MUNINN_")
            } | {
                "MUNINN_PYTHON": sys.executable,
                "MUNINN_HOME": str(self.root / "data"),
                "HOME": str(self.root),
            }

        def run_cmd(
            self, arguments: list[str]
        ) -> subprocess.CompletedProcess[str]:
            """Invoke cmd with standard Windows argument quoting."""
            command = subprocess.list2cmdline(
                [str(self.bin / "muninn.cmd"), *arguments]
            )
            shell = subprocess.list2cmdline(
                [os.environ.get("COMSPEC", "cmd.exe")]
            )
            return subprocess.run(
                shell + ' /d /s /c "' + command + '"',
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
            )

        def test_exact_roundtrip_quotes_empty_unicode_and_metacharacters(
            self,
        ) -> None:
            arguments = [
                "search",
                'embedded "quotes"',
                "",
                "& | < > ^ % !",
                "café 雪 owner's text",
                "--leading",
                "trailing\\",
            ]
            done = self.run_cmd(arguments)
            self.assertEqual(done.returncode, 17, done.stderr)
            self.assertEqual(done.stderr, "")
            self.assertEqual(json.loads(done.stdout), arguments)

        def test_missing_explicit_interpreter_falls_back_to_path(
            self,
        ) -> None:
            self.env["MUNINN_PYTHON"] = str(self.root / "missing.exe")
            done = self.run_cmd(["stats"])
            self.assertEqual(done.returncode, 17, done.stderr)
            self.assertEqual(json.loads(done.stdout), ["stats"])

        def install_selection(self, record: dict[str, str]) -> Path:
            """Install the synthetic receiver in a private pinned release."""
            lib = self.root / "lib"
            release = lib / ("a" * 40)
            for directory in (self.root, lib, release):
                platform_io.ensure_private_dir(directory)
            package = release / "muninn"
            (self.root / "muninn").rename(package)
            for name in (
                "platform_paths",
                "platform_io",
                "platform_windows",
                "platform_windows_security",
            ):
                shutil.copyfile(
                    ROOT / "muninn" / (name + ".py"), package / (name + ".py")
                )
            manifest = lib / "selection.json"
            manifest.write_text(json.dumps(record), encoding="utf-8")
            return manifest

        def test_missing_recorded_interpreter_accepts_valid_override(
            self,
        ) -> None:
            self.install_selection(
                {"sha": "a" * 40, "python": str(self.root / "missing.exe")}
            )
            done = self.run_cmd(["stats"])
            self.assertEqual(done.returncode, 17, done.stderr)
            self.assertEqual(json.loads(done.stdout), ["stats"])

        def test_missing_recorded_interpreter_uses_verified_path(self) -> None:
            self.install_selection(
                {"sha": "a" * 40, "python": str(self.root / "missing.exe")}
            )
            self.env.pop("MUNINN_PYTHON")
            self.env["PATH"] = (
                str(Path(sys.executable).parent)
                + os.pathsep
                + self.env["PATH"]
            )
            done = self.run_cmd(["stats"])
            self.assertEqual(done.returncode, 17, done.stderr)
            self.assertEqual(json.loads(done.stdout), ["stats"])

        def test_malformed_record_refuses_before_override_marker_executes(
            self,
        ) -> None:
            manifest = self.install_selection(
                {"sha": "a" * 40, "python": sys.executable}
            )
            executable, marker = self.root / "marker.exe", self.root / "marker"
            compile_marker(self, executable, marker)
            self.env["MUNINN_PYTHON"] = str(executable)
            self.env["MUNINN_TEST_MARKER"] = str(marker)
            for record in (
                {
                    "sha": "a" * 40,
                    "python": "relative-python.exe",
                },
                {"SHA": "a" * 40, "python": sys.executable},
                {"sha": "a" * 40, "Python": sys.executable},
            ):
                with self.subTest(fields=tuple(record)):
                    manifest.write_text(json.dumps(record), encoding="utf-8")
                    done = self.run_cmd(["stats"])
                    self.assertEqual((done.returncode, done.stdout), (127, ""))
                    self.assertFalse(marker.exists())

        def test_present_unsafe_override_refuses_without_marker_execution(
            self,
        ) -> None:
            executable, marker = self.root / "marker.exe", self.root / "marker"
            compile_marker(self, executable, marker)
            subprocess.run(
                ["icacls", str(executable), "/grant", "*S-1-1-0:(W)"],
                check=True,
                capture_output=True,
            )
            self.env["MUNINN_PYTHON"] = str(executable)
            self.env["MUNINN_TEST_MARKER"] = str(marker)
            done = self.run_cmd(["stats"])
            self.assertEqual((done.returncode, done.stdout), (127, ""))
            self.assertFalse(marker.exists())

    class NativeServeTests(unittest.TestCase):
        """The actual writer publishes identity and stops while idle."""

        def test_stale_request_cannot_stop_idle_replacement(self) -> None:
            with tempfile.TemporaryDirectory() as temp:
                home = Path(temp) / "data"
                platform_io.ensure_private_dir(home)
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("MUNINN_")
                }
                env |= {
                    "MUNINN_HOME": str(home),
                    "MUNINN_ROOTS": "{}",
                    "HOME": temp,
                }
                code = "from muninn.cli import main; raise SystemExit(main())"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-X",
                        "utf8",
                        "-c",
                        code,
                        "serve",
                        "--interval",
                        "30",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 20
                    status: dict[str, object] = {}
                    while time.monotonic() < deadline:
                        with contextlib.suppress(OSError, ValueError):
                            status = json.loads(
                                (home / "status.json").read_bytes()
                            )
                        if status.get("passes", 0):
                            break
                        if process.poll() is not None:
                            self.fail(str(process.communicate(timeout=2)))
                        time.sleep(0.05)
                    self.assertEqual(status.get("pid"), process.pid)
                    generation = str(status["stop_generation"])
                    poller_stop.request(home, process.pid, "b" * 32)
                    time.sleep(0.3)
                    self.assertIsNone(process.poll())
                    poller_stop.request(home, process.pid, generation)
                    out, err = process.communicate(timeout=5)
                    self.assertEqual(
                        (process.returncode, out, err), (0, b"", b"")
                    )
                    store.connect_ro(store.db_path(home)).close()
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
