"""Native bootstrap guards prevent replacement through ancestor sharing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from muninn import platform_io
from tests.ingest_support import ROOT

if sys.platform == "win32":

    class BootstrapGuardTests(unittest.TestCase):
        """Hold installed identities for an actual cmd-to-Python lifetime."""

        def test_installed_ancestry_and_metadata_cannot_be_replaced(
            self,
        ) -> None:
            with tempfile.TemporaryDirectory() as temp:
                ancestor = Path(temp) / "ancestor"
                base = ancestor / "Muninn"
                lib, bin_dir = base / "lib", base / "bin"
                for path in (ancestor, base, lib, bin_dir):
                    platform_io.ensure_private_dir(path)
                self.copy_bootstrap(bin_dir)
                sha = "a" * 40
                release = lib / sha
                platform_io.ensure_private_dir(release)
                shutil.copytree(ROOT / "muninn", release / "muninn")
                ready, stop, marker = (
                    Path(temp) / name
                    for name in (
                        "ready",
                        "stop",
                        "unvalidated-interpreter-marker",
                    )
                )
                self.write_waiting_cli(release)
                manifest = lib / "selection.json"
                fd = platform_io.open_private(manifest, os.O_CREAT | os.O_RDWR)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"sha": sha, "python": sys.executable}, handle)
                unvalidated = Path(temp) / "unvalidated.exe"
                compile_marker(self, unvalidated, marker)
                forged = Path(temp) / "forged.json"
                forged.write_text(
                    json.dumps({"sha": sha, "python": str(unvalidated)})
                )
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("MUNINN_")
                }
                env |= {
                    "HOME": temp,
                    "USERPROFILE": temp,
                    "MUNINN_HOME": str(base / "data"),
                    "MUNINN_TEST_MARKER": str(marker),
                }
                process = subprocess.Popen(
                    subprocess.list2cmdline([os.environ["COMSPEC"]])
                    + ' /d /s /c "'
                    + subprocess.list2cmdline(
                        [str(bin_dir / "muninn.cmd"), str(ready), str(stop)]
                    )
                    + '"',
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 90
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail(str(process.communicate(timeout=5)))
                        time.sleep(0.05)
                    self.assertTrue(
                        ready.exists(), "native bootstrap did not start"
                    )
                    for path in (ancestor, base, lib, release):
                        with self.assertRaises(OSError):
                            path.rename(path.with_name(path.name + "-swapped"))
                    with self.assertRaises(OSError):
                        forged.replace(manifest)
                    self.assertFalse(marker.exists())
                    self.assertEqual(
                        json.loads(manifest.read_bytes())["python"],
                        sys.executable,
                    )
                    stop.touch()
                    out, err = process.communicate(timeout=20)
                    self.assertEqual((process.returncode, err), (0, b""))
                    self.assertEqual(json.loads(out), {"ok": True})
                finally:
                    stop.touch()
                    if process.poll() is None:
                        try:
                            process.communicate(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate(timeout=5)

        def copy_bootstrap(self, bin_dir: Path) -> None:
            """Copy the isolated stable launcher into the synthetic install."""
            for name in ("muninn.cmd", "muninn.ps1"):
                shutil.copyfile(ROOT / "bin" / name, bin_dir / name)
            diagnose_bootstrap(bin_dir)

        def write_waiting_cli(self, release: Path) -> None:
            """Create a synthetic writer that exposes its guarded lifetime."""
            (release / "muninn" / "cli.py").write_text(
                '"""Synthetic waiting writer."""\n'
                "import json, time\nfrom pathlib import Path\n"
                "def main(args: list[str]) -> int:\n"
                '    """Expose a bounded writer lifetime."""\n'
                "    Path(args[0]).touch()\n"
                "    end = time.monotonic() + 90\n"
                "    while not Path(args[1]).exists():\n"
                "        if time.monotonic() >= end: return 19\n"
                "        time.sleep(0.05)\n"
                '    print(json.dumps({"ok": True}))\n    return 0\n',
                encoding="utf-8",
            )


def compile_marker(
    case: unittest.TestCase, unvalidated: Path, marker: Path
) -> None:
    """Compile an unvalidated marker executable in the fixture."""
    compile_env = os.environ | {
        "MUNINN_TEST_EXE": str(unvalidated),
        "MUNINN_TEST_MARKER": str(marker),
    }
    source = (
        "using System; using System.IO; public class Marker {"
        "public static void Main() { File.WriteAllText("
        "Environment.GetEnvironmentVariable("
        '"MUNINN_TEST_MARKER"), '
        '"unvalidated"); }}'
    )
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Add-Type -TypeDefinition '" + source + "' "
            "-OutputAssembly $env:MUNINN_TEST_EXE "
            "-OutputType ConsoleApplication",
        ],
        check=True,
        capture_output=True,
        env=compile_env,
        timeout=90,
    )

    subprocess.run([str(unvalidated)], env=compile_env, check=True, timeout=30)
    case.assertEqual(marker.read_text(), "unvalidated")
    marker.unlink()


def diagnose_bootstrap(bin_dir: Path) -> None:
    """Expose refusal exceptions only in synthetic launcher copies."""
    script = bin_dir / "muninn.ps1"
    source = script.read_text(encoding="utf-8")
    marker = "    [Console]::Error.WriteLine('muninn: native launcher refused"
    source = source.replace(
        marker,
        "    [Console]::Error.WriteLine($_.Exception.ToString())\n"
        "    [Console]::Error.WriteLine($_.ScriptStackTrace)\n" + marker,
    )
    script.write_text(source, encoding="utf-8")
