"""Route the leave flag through the installer command line and wrapper."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from install import entry, failure_text, installer
from install.context import Ctx, StepFailedError
from tests.installer_support import ROOT, World, git


class TtyInput(io.StringIO):
    """Standard input that says that it is a terminal."""

    def isatty(self) -> bool:
        """Report a terminal."""
        return True


class LeaveEntryTests(unittest.TestCase):
    """Route the leave flag through the installer command line."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.calls: list[tuple[bool, bool]] = []

    def run_main(
        self, argv: list[str], stdin: io.StringIO, outcome: str | None
    ) -> tuple[int, list[str], str]:
        """Run the installer command with a fake leave function.

        Args:
            argv: The installer arguments.
            stdin: The standard input of the run.
            outcome: The fake result, or None to refuse with a code.

        Returns:
            The exit code, the printed lines and the error text.
        """

        def leave(ctx: Ctx, prompt: Callable[[str], str] | None) -> str:
            self.calls.append((ctx.dry_run, prompt is not None))
            if outcome is None:
                raise StepFailedError("leave_links_changed")
            return outcome

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch("install.leave_start.leave", side_effect=leave),
            mock.patch.object(entry.sys, "stdin", stdin),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = installer.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_check_form_is_a_dry_run(self) -> None:
        for argv in (
            ["--leave-unfinished", "--check"],
            ["--check", "--leave-unfinished"],
            ["--leave-unfinished", "--dry-run"],
        ):
            with self.subTest(argv=argv):
                self.calls.clear()
                code, _, _ = self.run_main(argv, io.StringIO(), "would_leave")
                self.assertEqual(code, 0)
                self.assertEqual(self.calls, [(True, False)])

    def test_real_form_asks_only_on_a_terminal(self) -> None:
        code, _, _ = self.run_main(["--leave-unfinished"], TtyInput(), "left")
        self.assertEqual((code, self.calls), (0, [(False, True)]))
        self.calls.clear()
        code, _, _ = self.run_main(
            ["--leave-unfinished"], io.StringIO(), "nothing"
        )
        self.assertEqual((code, self.calls), (0, [(False, False)]))

    def test_refusal_prints_the_code_and_its_text(self) -> None:
        code, lines, _ = self.run_main(
            ["--leave-unfinished"], io.StringIO(), None
        )
        self.assertEqual(code, 1)
        self.assertIn("FAILED: leave_links_changed", lines)
        after = lines[lines.index("FAILED: leave_links_changed") + 1 :]
        self.assertEqual(after, failure_text.explain("leave_links_changed"))

    def test_other_arguments_are_a_usage_error_of_the_leave_route(
        self,
    ) -> None:
        for argv, other in (
            (["--leave-unfinished", "--fresh"], "--fresh"),
            (["--leave-unfinished", "--upgrade", "--check"], "--upgrade"),
        ):
            with self.subTest(argv=argv):
                self.calls.clear()
                code, _, err = self.run_main(argv, io.StringIO(), "left")
                self.assertEqual(code, 2)
                self.assertEqual(self.calls, [])
                message = err.strip().splitlines()[-1]
                self.assertIn(other, message)
                self.assertNotIn("--leave-unfinished", message)
                self.assertNotIn("required", message)

    def test_native_entry_uses_the_same_route(self) -> None:
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch(
                "install.leave_start.leave", return_value="would_leave"
            ) as leave,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = entry.main(["--leave-unfinished", "--check"])
        self.assertEqual(code, 0)
        leave.assert_called_once()
        self.assertTrue(leave.call_args.args[0].dry_run)


class LeaveWrapperTests(unittest.TestCase):
    """The shell wrapper sends the leave flag on without other setup."""

    def test_wrapper_forwards_only_the_arguments_of_the_person(self) -> None:
        world = World(self)
        script = world.repo / "bin/muninn-install"
        shutil.copyfile(ROOT / "bin/muninn-install", script)
        script.chmod(0o755)
        git(world.repo, "add", "bin/muninn-install")
        git(world.repo, "commit", "-qm", "synthetic wrapper")
        python = world.home.parent / "fake-python"
        python.write_text(
            '#!/bin/sh\ncase "$*" in\n*"print(sys.platf'
            'orm)"*) echo darwin ;;\n*"-m install.inst'
            'aller"*) printf "%s\\n" "$@" ;;\n*) exit 0'
            " ;;\nesac\n"
        )
        python.chmod(0o755)
        for argv in (
            ["--leave-unfinished"],
            ["--leave-unfinished", "--check"],
            ["--check", "--leave-unfinished"],
        ):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [str(script), *argv],
                    env=dict(
                        os.environ,
                        HOME=str(world.home),
                        MUNINN_PYTHON=str(python),
                    ),
                    input="",
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Enable this checkout", result.stderr)
                lines = result.stdout.splitlines()
                self.assertIn("install.installer", lines)
                start = lines.index("install.installer") + 1
                self.assertEqual(lines[start:], argv)


if __name__ == "__main__":
    unittest.main()
