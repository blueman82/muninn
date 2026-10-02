"""The parallel test runner reports counts and failures correctly."""

from __future__ import annotations

import contextlib
import io
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
