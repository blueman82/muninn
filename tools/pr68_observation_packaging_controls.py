"""Exercise the isolated bin-plus-muninn version-launch contract."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PackagingControls(unittest.TestCase):
    """Use the existing isolated release recipe with the reviewed overlay."""

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher recipe")
    def test_overlaid_release_version_needs_no_tools_package(self) -> None:
        """Launch only version output from a bin-plus-muninn temp release."""
        plist = plistlib.loads(
            (ROOT / "launchd/com.muninn.plist").read_bytes()
        )
        with tempfile.TemporaryDirectory() as temporary:
            home = os.path.realpath(temporary)
            release = Path(home, ".local/lib/muninn/abc1234")
            shutil.copytree(ROOT / "bin", release / "bin")
            shutil.copytree(
                ROOT / "muninn",
                release / "muninn",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy2(
                ROOT
                / "tools/pr68_observation_payload"
                / "review_process_quiescence.py",
                release / "muninn/review_process_quiescence.py",
            )
            collector = ROOT / "tools/_cleanup_observation.py"
            if (
                "from muninn import _cleanup_observation"
                in (
                    release / "muninn/review_process_quiescence.py"
                ).read_text()
            ):
                shutil.copy2(
                    collector, release / "muninn/_cleanup_observation.py"
                )
            (release.parent / "current").symlink_to(release)
            (release.parent / "python").symlink_to(sys.executable)
            program = plist["ProgramArguments"][0].replace("@HOME@", home)
            self.assertEqual(
                os.path.realpath(program), str(release / "bin/muninn")
            )
            self.assertTrue(os.access(program, os.X_OK))
            self.assertFalse((release / "tools").exists())
            self.assertIn('"$py" -I -B', (release / "bin/muninn").read_text())
            result = subprocess.run(
                [program, "--version"],
                cwd="/",
                env=plist["EnvironmentVariables"]
                | {"HOME": home, "MUNINN_PYTHON": sys.executable},
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(
                json.dumps(
                    {
                        "argv": [program, "--version"],
                        "cwd": "/",
                        "home": home,
                        "tools_present": (release / "tools").exists(),
                        "actual_exit": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                ),
                flush=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
