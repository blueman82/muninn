"""The parallel test runner reports counts and failures correctly."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import run_tests

ROOT = Path(__file__).resolve().parent.parent


class ParseTest(unittest.TestCase):
    """Reading unittest's summary line."""

    def test_counts_one_and_many_tests(self) -> None:
        self.assertEqual(run_tests.parse_ran("Ran 1 test in 0.1s\n"), 1)
        self.assertEqual(run_tests.parse_ran("x\nRan 42 tests in 3.0s\n"), 42)

    def test_no_summary_means_zero(self) -> None:
        self.assertEqual(run_tests.parse_ran("Traceback ..."), 0)


class DiscoveryTest(unittest.TestCase):
    """Which modules the runner would run."""

    def test_lists_every_test_file_and_nothing_else(self) -> None:
        modules = run_tests.test_modules(ROOT)
        on_disk = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
        self.assertEqual(modules, [f"tests.{stem}" for stem in on_disk])
        self.assertNotIn("tests.installer_support", modules)

    def test_default_jobs_is_bounded(self) -> None:
        jobs = run_tests.default_jobs()
        self.assertGreaterEqual(jobs, 1)
        self.assertLessEqual(jobs, run_tests.DEFAULT_JOBS_CAP)


class CleanEnvironmentTest(unittest.TestCase):
    """Tests never inherit the git variables a commit hook exports."""

    def test_repo_locating_variables_are_removed_and_others_kept(self) -> None:
        env = {"GIT_DIR": "/x", "GIT_INDEX_FILE": "/y", "PATH": "/bin"}
        self.assertEqual(run_tests.clean_environment(env), {"PATH": "/bin"})

    def git(self, cwd: Path, *args: str, env: dict[str, str]) -> str:
        """Run git quietly in a throwaway directory.

        Args:
            cwd: Directory to run in.
            *args: Arguments after ``git``.
            env: The environment to run with.

        Returns:
            Trimmed standard output.
        """
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def test_git_init_under_a_worktree_hook_env_cannot_flip_bare(self) -> None:
        # Reproduces the real incident in a temp repo: with the worktree's
        # GIT_DIR exported, `git init` marks the SHARED repository bare.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            plain = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            self.git(base, "init", "-q", "main", env=plain)
            main = base / "main"
            self.git(
                main, "commit", "-q", "--allow-empty", "-m", "i", env=plain
            )
            self.git(
                main, "worktree", "add", "-q", "../wt", "-b", "s", env=plain
            )
            gitdir = self.git(
                base / "wt", "rev-parse", "--absolute-git-dir", env=plain
            )
            (base / "scratch").mkdir()
            hook_env = run_tests.clean_environment(
                {**plain, "GIT_DIR": gitdir}
            )
            self.git(base / "scratch", "init", "-q", env=hook_env)
            bare = self.git(main, "config", "core.bare", env=plain)
            self.assertEqual(bare, "false")


class ReportTest(unittest.TestCase):
    """Exit status of the summary."""

    @staticmethod
    def result(code: int, ran: int) -> run_tests.ModuleResult:
        """Build a module result.

        Args:
            code: The interpreter's exit status.
            ran: Tests reported as run.

        Returns:
            A result with fixed text and timing.
        """
        return run_tests.ModuleResult("tests.x", code, ran, 0.1, "out")

    @staticmethod
    def status(*results: run_tests.ModuleResult) -> int:
        """Run ``report`` with its printing captured.

        Args:
            *results: The module results to summarise.

        Returns:
            The exit status ``report`` returns.
        """
        with contextlib.redirect_stdout(io.StringIO()):
            return run_tests.report(results, 1.0)

    def test_passes_when_every_module_passes(self) -> None:
        self.assertEqual(self.status(self.result(0, 3)), 0)

    def test_fails_on_a_failed_module(self) -> None:
        self.assertEqual(self.status(self.result(0, 3), self.result(1, 2)), 1)

    def test_a_module_with_no_tests_is_not_a_failure(self) -> None:
        shim = self.result(run_tests.NO_TESTS_EXIT, 0)
        self.assertEqual(self.status(shim, self.result(0, 3)), 0)

    def test_exit_five_with_tests_run_is_still_a_failure(self) -> None:
        self.assertEqual(self.status(self.result(5, 2)), 1)

    def test_fails_when_nothing_ran(self) -> None:
        self.assertEqual(self.status(self.result(0, 0)), 1)


class RunModuleTest(unittest.TestCase):
    """Running a real module in its own interpreter."""

    def test_runs_a_small_module_and_counts_its_tests(self) -> None:
        # Never this module: it would spawn itself without end.
        result = run_tests.run_module("tests.test_standards_rules", ROOT)
        self.assertEqual(result.returncode, 0, result.output)
        self.assertGreaterEqual(result.ran, 15)


if __name__ == "__main__":
    unittest.main()
