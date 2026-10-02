"""bin/muninn-install picks the install mode and passes the right flags."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.installer_support import ROOT, git

WRAPPER = ROOT / "bin/muninn-install"


class WrapperTest(unittest.TestCase):
    """Run the wrapper in a temp HOME with a stub in place of Python."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.log = Path(tmp.name) / "argv"
        self.stub = Path(tmp.name) / "python-stub"
        self.stub.write_text(
            f'#!/bin/sh\necho "$@" >> {self.log}\nexit "${{STUB_RC:-0}}"\n'
        )
        self.stub.chmod(0o755)
        self.sha = git(ROOT, "rev-parse", "HEAD").decode().strip()
        self.lib = self.home / ".local/lib/muninn"
        self.data = self.home / ".local/share/muninn"

    def run_wrapper(
        self, *args: str, rc: int = 0, script: Path = WRAPPER
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``bin`` script against the temp HOME.

        Args:
            *args: Command-line arguments.
            rc: Exit status the stub Python should return.
            script: The script to run, ``bin/muninn-install`` by default.

        Returns:
            The finished process, with text output.
        """
        env = dict(
            os.environ,
            HOME=str(self.home),
            MUNINN_PYTHON=str(self.stub),
            STUB_RC=str(rc),
        )
        return subprocess.run(
            [script, *args], env=env, capture_output=True, text=True
        )

    def installed(self, sha: str) -> None:
        """Make the temp HOME look like a machine running release ``sha``."""
        (self.lib / sha).mkdir(parents=True)
        (self.lib / "current").symlink_to(sha)
        self.data.mkdir(parents=True)

    def argv(self) -> str:
        """Return what the stub Python was run with, or empty if never."""
        return self.log.read_text() if self.log.exists() else ""

    def test_bare_on_an_empty_machine_runs_a_fresh_install(self) -> None:
        r = self.run_wrapper()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--fresh", self.argv())
        self.assertNotIn("--dry-run", self.argv())
        self.assertIn(f"--sha {self.sha}", self.argv())
        self.assertIn(f"installed {self.sha[:7]} (fresh)", r.stdout)

    def test_bare_on_an_installed_machine_upgrades(self) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper()
        self.assertIn("--upgrade", self.argv())
        self.assertIn(f"installed {self.sha[:7]} (upgrade)", r.stdout)

    def test_the_summary_line_names_the_poller_pid(self) -> None:
        self.installed("a" * 40)
        (self.data / "status.json").write_text('{"passes": 1, "pid": 4242}')
        r = self.run_wrapper()
        self.assertIn("(upgrade); poller pid 4242; doctor ok;", r.stdout)

    def test_the_summary_line_survives_a_missing_status_file(self) -> None:
        r = self.run_wrapper()
        self.assertIn("poller pid unknown", r.stdout)

    def test_uninstall_passes_its_flags_to_the_uninstaller(self) -> None:
        r = self.run_wrapper("--uninstall", "--dry-run", "--purge-data")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.argv().split()[-4:],
            ["-m", "install.uninstall", "--dry-run", "--purge-data"],
        )

    def test_uninstall_runs_on_a_half_installed_machine(self) -> None:
        self.data.mkdir(parents=True)
        r = self.run_wrapper("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install.uninstall", self.argv())

    def test_uninstall_needs_neither_git_nor_a_commit(self) -> None:
        bare = self.home.parent / "tarball/bin"
        bare.mkdir(parents=True)
        shutil.copy2(WRAPPER, bare / "muninn-install")
        r = self.run_wrapper("--uninstall", script=bare / "muninn-install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-m install.uninstall", self.argv())

    def test_the_uninstall_command_is_the_uninstall_flag(self) -> None:
        r = self.run_wrapper("--dry-run", script=ROOT / "bin/muninn-uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-m install.uninstall --dry-run", self.argv())

    def test_help_describes_the_wrapper_and_writes_nothing(self) -> None:
        for flag in ("--help", "-h"):
            r = self.run_wrapper(flag)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("--check", r.stdout)
            self.assertIn("--status", r.stdout)
        self.assertEqual(self.argv(), "")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failed_install_prints_no_success_line(self) -> None:
        r = self.run_wrapper(rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("installed", r.stdout)

    def test_half_an_install_is_refused_before_anything_runs(self) -> None:
        self.data.mkdir(parents=True)  # data without a current release
        r = self.run_wrapper()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("half-installed", r.stderr)
        self.assertEqual(self.argv(), "")

    def test_check_is_a_dry_run_that_ends_with_the_command_to_run(
        self,
    ) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper("--check")
        self.assertIn("--dry-run", self.argv())
        self.assertIn("Upgrade: aaaaaaa", r.stdout)
        self.assertTrue(r.stdout.endswith("OK to run: bin/muninn-install\n"))

    def test_check_writes_nothing_to_the_home(self) -> None:
        self.run_wrapper("--check")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failed_check_does_not_say_ok(self) -> None:
        r = self.run_wrapper("--check", rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("OK to run", r.stdout)

    def test_status_compares_the_installed_commit_with_head(self) -> None:
        self.installed("a" * 40)
        r = self.run_wrapper("--status")
        self.assertIn(f"installed aaaaaaa, checkout {self.sha[:7]}", r.stdout)
        self.assertIn("upgrade needed", r.stdout)
        self.assertEqual(self.argv(), "")

    def test_status_says_up_to_date_when_they_match(self) -> None:
        self.installed(self.sha)
        self.assertIn("up to date", self.run_wrapper("--status").stdout)

    def test_status_on_an_empty_machine_says_not_installed(self) -> None:
        self.assertIn("not installed", self.run_wrapper("--status").stdout)

    def test_other_arguments_go_to_the_installer_unchanged(self) -> None:
        self.run_wrapper("--repo", "/x", "--sha", "f" * 40, "--fresh")
        self.assertIn(f"--repo /x --sha {'f' * 40} --fresh", self.argv())


if __name__ == "__main__":
    unittest.main()
