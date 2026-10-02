"""Run the unittest modules in parallel processes.

``python3.13 -m tools.run_tests [-j N]`` is the fast path to the same result as
``python3.13 -m unittest discover -s tests -t .``. Each test module runs in its
own interpreter, so tests never share process state; threads only supervise the
subprocesses. Threads inside one interpreter would not help: the suite is
Python code under the GIL plus process start-up, and the tests use temporary
directories and their own subprocesses, which is safe to run side by side.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_CAP = 12
# unittest exits 5 for a module that defines no tests (a re-export shim).
NO_TESTS_EXIT = 5
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
# Variables git exports to a hook. Committing from a linked worktree sets them
# to paths inside the shared .git, and a test that runs `git init` in a temp
# directory would then flip the real repository's core.bare to true.
GIT_REPO_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_NAMESPACE",
)


@dataclass(frozen=True)
class ModuleResult:
    """The outcome of running one test module.

    Attributes:
        module: Dotted module name, such as ``tests.test_store``.
        returncode: The interpreter's exit status.
        ran: How many tests unittest reported running.
        seconds: Wall-clock time for the module.
        output: Combined standard output and standard error.
    """

    module: str
    returncode: int
    ran: int
    seconds: float
    output: str


def test_modules(root: Path = ROOT) -> list[str]:
    """List the test modules unittest discovery would load.

    Args:
        root: Repository root.

    Returns:
        Dotted names of every ``tests/test_*.py`` file, sorted.
    """
    return sorted(
        f"tests.{p.stem}" for p in (root / "tests").glob("test_*.py")
    )


def parse_ran(output: str) -> int:
    """Read the test count from unittest's summary line.

    Args:
        output: Text printed by ``python -m unittest``.

    Returns:
        The number after ``Ran``, or 0 when there is no summary line.
    """
    match = RAN.search(output)
    return int(match[1]) if match else 0


def clean_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without the variables that locate a git repository.

    Args:
        env: The environment to copy, normally ``os.environ``.

    Returns:
        The same variables minus ``GIT_REPO_VARS``.
    """
    return {k: v for k, v in env.items() if k not in GIT_REPO_VARS}


def run_module(module: str, root: Path = ROOT) -> ModuleResult:
    """Run one test module in a fresh interpreter.

    Args:
        module: Dotted module name.
        root: Directory to run in, so ``tests`` is importable.

    Returns:
        The module's result.
    """
    start = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=root,
        env=clean_environment(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    output = done.stdout + done.stderr
    return ModuleResult(
        module,
        done.returncode,
        parse_ran(output),
        time.monotonic() - start,
        output,
    )


def default_jobs() -> int:
    """Pick a worker count: the CPU count, capped to leave the machine usable.

    Returns:
        At least 1 and at most ``DEFAULT_JOBS_CAP``.
    """
    return max(1, min(os.cpu_count() or 1, DEFAULT_JOBS_CAP))


def run_all(
    modules: Sequence[str], jobs: int, root: Path = ROOT
) -> list[ModuleResult]:
    """Run every module, ``jobs`` at a time.

    Args:
        modules: Dotted module names.
        jobs: Maximum number of interpreters running at once.
        root: Directory to run in.

    Returns:
        One result per module, in the order given.
    """
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(
            pool.map(functools.partial(run_module, root=root), modules)
        )


def passed(result: ModuleResult) -> bool:
    """Say whether a module's run counts as a pass.

    Args:
        result: One module's result.

    Returns:
        True for exit 0, and for exit 5 when the module simply has no tests.
    """
    return result.returncode == 0 or (
        result.returncode == NO_TESTS_EXIT and result.ran == 0
    )


def report(results: Sequence[ModuleResult], seconds: float) -> int:
    """Print failures and a summary.

    Args:
        results: Every module's result.
        seconds: Wall-clock time for the whole run.

    Returns:
        0 when every module passed and at least one test ran, else 1.
    """
    failed = [r for r in results if not passed(r)]
    for result in failed:
        print(f"=== {result.module} (exit {result.returncode})")
        print(result.output.rstrip())
    total = sum(r.ran for r in results)
    print(
        f"tests: ran {total} in {len(results)} modules in {seconds:.1f}s; "
        f"{len(failed)} module(s) failed"
    )
    return 1 if failed or total == 0 else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the whole suite in parallel.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        0 when every test passed, otherwise 1.
    """
    parser = argparse.ArgumentParser(prog="tools.run_tests")
    parser.add_argument("-j", "--jobs", type=int, default=default_jobs())
    args = parser.parse_args(argv)
    start = time.monotonic()
    results = run_all(test_modules(), max(1, args.jobs))
    return report(results, time.monotonic() - start)


if __name__ == "__main__":
    sys.exit(main())
