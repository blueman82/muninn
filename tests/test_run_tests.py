"""The parallel test runner reports counts and failures correctly."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_default_worker_limit_is_four(self) -> None:
        with mock.patch.object(run_tests.os, "cpu_count", return_value=16):
            self.assertEqual(run_tests.default_jobs(), 4)


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

    def test_standards_gate_remains_usable_without_site_packages(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", "-m", "tools.check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DeadlineTest(unittest.TestCase):
    """Bound actual modules and contain their inherited children."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").touch()

    def module(self, name: str, source: str) -> str:
        """Write one disposable unittest module and return its dotted name."""
        (self.root / "tests" / f"test_{name}.py").write_text(source)
        return f"tests.test_{name}"

    def test_hang_retains_partial_output_and_fails_report(self) -> None:
        module = self.module(
            "hang",
            "import time, sys\nprint('partial stdout', flush=True)\n"
            "print('partial stderr', file=sys.stderr, flush=True)\n"
            "time.sleep(60)\n",
        )
        start = time.monotonic()
        result = run_tests.run_module(module, self.root, timeout=1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(run_tests.passed(result))
        self.assertIn("partial stdout", result.output)
        self.assertIn("partial stderr", result.output)
        self.assertIn("timed out", result.output)
        self.assertIn(module, result.output)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_tests.report([result], 1), 1)
        self.assertIn(module, output.getvalue())

    def test_timeout_kills_child_with_inherited_output(self) -> None:
        marker = self.root / (
            "survived" if os.name == "nt" else r"C:\Users\survived"
        )
        ready = self.root / "ready"
        child_code = (
            f"import time,pathlib; pathlib.Path({str(ready)!r}).touch(); "
            f"time.sleep(3); pathlib.Path({str(marker)!r}).touch()"
        )
        module = self.module(
            "child",
            "import pathlib,subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            f"while not pathlib.Path({str(ready)!r}).exists():\n"
            " time.sleep(.01)\n"
            "print('child launched', flush=True)\ntime.sleep(60)\n",
        )
        result = run_tests.run_module(module, self.root, timeout=1)
        self.assertTrue(ready.exists(), result.output)
        self.assertIn("child launched", result.output)
        self.assertFalse(run_tests.passed(result))
        time.sleep(3)
        self.assertFalse(marker.exists(), result.output)

    def test_finished_module_cleans_child_without_changing_success(
        self,
    ) -> None:
        marker = self.root / (
            "survived_success" if os.name == "nt" else r"C:\Users\success"
        )
        ready = self.root / "ready_success"
        child_code = (
            f"import time,pathlib; pathlib.Path({str(ready)!r}).touch(); "
            f"time.sleep(4); pathlib.Path({str(marker)!r}).touch()"
        )
        module = self.module(
            "finished_child",
            "import pathlib,subprocess,sys,time,unittest\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            f"while not pathlib.Path({str(ready)!r}).exists():\n"
            " time.sleep(.01)\n"
            "class T(unittest.TestCase):\n"
            " def test_ok(self): self.assertTrue(True)\n",
        )
        result = run_tests.run_module(module, self.root, timeout=2)
        self.assertTrue(ready.exists(), result.output)
        self.assertEqual(
            (result.returncode, result.ran), (0, 1), result.output
        )
        time.sleep(4)
        self.assertFalse(marker.exists(), result.output)

    def test_other_modules_finish_after_a_timeout(self) -> None:
        hang = self.module("hang", "import time\ntime.sleep(60)\n")
        good = self.module(
            "good",
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_ok(self): self.assertTrue(True)\n",
        )
        results = run_tests.run_all([hang, good], 2, self.root, timeout=1)
        self.assertEqual([r.module for r in results], [hang, good])
        self.assertFalse(run_tests.passed(results[0]))
        self.assertTrue(run_tests.passed(results[1]), results[1].output)
        self.assertEqual(results[1].ran, 1)

    def test_main_returns_nonzero_and_identifies_hanging_module(self) -> None:
        module = self.module("main_hang", "import time\ntime.sleep(60)\n")
        real_run = run_tests.run_all
        with (
            mock.patch.object(
                run_tests, "test_modules", return_value=[module]
            ),
            mock.patch.object(
                run_tests,
                "run_all",
                side_effect=lambda modules, jobs, timeout: real_run(
                    modules, jobs, self.root, timeout=timeout
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            status = run_tests.main(["--timeout=1", "-j", "2"])
        self.assertEqual(status, 1)
        self.assertIn(module, output.getvalue())
        self.assertIn("timed out", output.getvalue())

    def test_killed_module_is_a_failure_without_waiting_for_deadline(
        self,
    ) -> None:
        module = self.module(
            "killed", "import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n"
        )
        result = run_tests.run_module(module, self.root, timeout=5)
        self.assertFalse(run_tests.passed(result))
        self.assertNotIn("timed out", result.output)

    def test_failure_and_no_test_counts_are_preserved(self) -> None:
        bad = self.module(
            "bad",
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_bad(self): self.fail('expected failure')\n",
        )
        shim = self.module("shim", "")
        failed = run_tests.run_module(bad, self.root, timeout=5)
        empty = run_tests.run_module(shim, self.root, timeout=5)
        self.assertEqual((failed.returncode, failed.ran), (1, 1))
        self.assertIn("expected failure", failed.output)
        self.assertTrue(run_tests.passed(empty), empty.output)
        self.assertEqual(empty.ran, 0)

    def test_large_separate_streams_keep_output_and_test_count(self) -> None:
        module = self.module(
            "output",
            "import sys,unittest\n"
            "print('A'*200000, flush=True)\n"
            "print('B'*200000, file=sys.stderr, flush=True)\n"
            "class T(unittest.TestCase):\n"
            " def test_ok(self): self.assertTrue(True)\n",
        )
        result = run_tests.run_module(module, self.root, timeout=10)
        self.assertEqual((result.returncode, result.ran), (0, 1))
        self.assertIn("A" * 200000, result.output)
        self.assertIn("B" * 200000, result.output)
        self.assertLess(
            result.output.index("A" * 20), result.output.index("B" * 20)
        )

    def test_launch_failure_is_a_failed_module_result(self) -> None:
        with mock.patch.object(
            run_tests.sys, "executable", str(self.root / "absent")
        ):
            result = run_tests.run_module("tests.absent", self.root)
        self.assertFalse(run_tests.passed(result))
        self.assertIn("launch failed", result.output)

    def test_module_exit_codes_do_not_claim_timeout_or_cancellation(
        self,
    ) -> None:
        for code in (124, 130):
            module = self.module(f"exit_{code}", f"raise SystemExit({code})\n")
            result = run_tests.run_module(module, self.root, timeout=5)
            self.assertEqual(result.returncode, code)
            self.assertNotIn("timed out", result.output)
            self.assertNotIn("cancelled", result.output)

    def test_cancelled_module_never_launches(self) -> None:
        cancel = threading.Event()
        cancel.set()
        module = self.module("cancel", "raise AssertionError('launched')\n")
        result = run_tests.run_module(
            module, self.root, timeout=5, cancel=cancel
        )
        self.assertFalse(run_tests.passed(result))
        self.assertIn("cancelled", result.output)
        self.assertNotIn("AssertionError", result.output)

    def test_cancel_stops_an_active_module(self) -> None:
        cancel = threading.Event()
        module = self.module("active", "import time\ntime.sleep(60)\n")
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        result = run_tests.run_module(
            module, self.root, timeout=30, cancel=cancel
        )
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("cancelled", result.output)

    def test_api_rejects_nonfinite_and_nonpositive_timeouts(self) -> None:
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_tests.run_module("tests.absent", self.root, timeout=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_tests.run_all([], 1, self.root, timeout=value)

    @unittest.skipIf(os.name == "nt", "SIGINT delivery is POSIX-specific")
    def test_interrupt_cancels_running_and_queued_modules(self) -> None:
        ready = self.root / "ready"
        queued = self.root / "queued"
        hang = self.module(
            "interrupt",
            "import pathlib,time\n"
            f"pathlib.Path({str(ready)!r}).touch()\ntime.sleep(10)\n",
        )
        other = self.module(
            "queued",
            f"import pathlib\npathlib.Path({str(queued)!r}).touch()\n",
        )
        driver = (
            "from tools import run_tests as r\n"
            "from pathlib import Path\nimport functools,sys\n"
            f"r.test_modules=lambda: [{hang!r}, {other!r}]\n"
            f"r.run_all=functools.partial(r.run_all,root=Path({str(self.root)!r}))\n"
            "sys.exit(r.main(['-j','1','--timeout','30']))\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", driver],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stdout + stderr)
                self.assertIn(b"cancelled", stderr)
                self.assertFalse(queued.exists())
            finally:
                if process.poll() is None:
                    process.kill()

    def test_invalid_timeout_is_rejected_before_discovery(self) -> None:
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                run_tests.main([f"--timeout={value}"])
            self.assertEqual(error.exception.code, 2)

    def test_empty_discovery_is_a_failure(self) -> None:
        with (
            mock.patch.object(run_tests, "test_modules", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(run_tests.main([]), 1)


if __name__ == "__main__":
    unittest.main()
