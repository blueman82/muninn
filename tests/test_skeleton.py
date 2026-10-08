"""Skeleton contract: the muninn entry point and its bin/muninn launcher."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from muninn.cli import main
from tests.hook_support import launcher_args

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "muninn"
VERSION_LINE = "muninn 0.1.0\n"


def call_main(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run muninn.cli.main in-process.

    Args:
        argv: Command-line arguments after the program name.

    Returns:
        The exit code, stdout and stderr.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def run(
    cmd: Sequence[str], cwd: str | Path
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured text output and a timeout.

    Args:
        cmd: Program and arguments.
        cwd: Working directory.

    Returns:
        The finished process.
    """
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=30
    )


class MainTests(unittest.TestCase):
    """muninn.cli.main answers --version and rejects anything else."""

    def test_version_returns_zero_and_prints_version(self) -> None:
        code, out, err = call_main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out, VERSION_LINE)
        self.assertEqual(err, "")

    def test_anything_else_returns_two_with_usage(self) -> None:
        cases = (
            [],
            ["frobnicate"],
            ["--frobnicate"],
            ["--ver"],
            ["--help"],
            ["--version", "extra"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = call_main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("usage:", err)


class LauncherTests(unittest.TestCase):
    """CLI behavior runs via POSIX shell or isolated native Python."""

    def test_version_from_foreign_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(launcher_args("--version"), cwd=tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, VERSION_LINE)

    def test_exit_code_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(launcher_args("frobnicate"), cwd=tmp)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)

    def test_ignores_stdlib_shadowing_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "argparse.py").write_text("raise RuntimeError\n")
            proc = run(launcher_args("--version"), cwd=tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, VERSION_LINE)

    def test_module_entry_point(self) -> None:
        cmd = [sys.executable, "-B", "-m", "muninn", "--version"]
        proc = run(cmd, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, VERSION_LINE)


if sys.platform != "win32":

    class PosixSymlinkTests(unittest.TestCase):
        """The POSIX launcher accepts a launcher symlink from another cwd."""

        def test_version_via_symlink(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                link = Path(tmp) / "muninn-link"
                link.symlink_to(LAUNCHER)
                proc = run([str(link), "--version"], cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, VERSION_LINE)


if __name__ == "__main__":
    unittest.main()
