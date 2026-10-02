"""The repository passes its own standards, and the gate cannot be removed."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tools import check
from tools.check import hooks_installed
from tools.standards import check_repo

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "pre-commit"
CLAUDE_SETTINGS = ROOT / ".claude" / "settings.json"


class RepositoryStandardsTest(unittest.TestCase):
    """Every checked file satisfies rules S1 to S8."""

    def test_repository_has_no_violations(self) -> None:
        found = check_repo(ROOT)
        shown = [str(v) for v in found[:40]]
        self.assertEqual(
            shown,
            [],
            f"{len(found)} violation(s); run: python3.13 -m tools.check",
        )


class GateCannotBeRemovedTest(unittest.TestCase):
    """The hooks and pins that make the standards mechanical stay in place."""

    def test_git_pre_commit_hook_runs_the_full_gate(self) -> None:
        self.assertTrue(HOOK.is_file(), "missing .githooks/pre-commit")
        self.assertTrue(os.access(HOOK, os.X_OK), "hook is not executable")
        text = HOOK.read_text(encoding="utf-8")
        self.assertIn("tools.check --full", text)
        self.assertNotIn("exit 0", text)

    def test_claude_code_blocks_stopping_on_a_failing_gate(self) -> None:
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        hooks = settings["hooks"]
        stop = json.dumps(hooks["Stop"])
        self.assertIn("tools.check --full", stop)
        self.assertIn("exit 2", stop)
        edit = json.dumps(hooks["PostToolUse"])
        self.assertIn("tools.check", edit)
        self.assertIn("Edit", edit)

    def test_git_hooks_are_switched_on(self) -> None:
        self.assertTrue(
            hooks_installed(),
            "run: git config core.hooksPath .githooks",
        )

    def test_the_full_gate_runs_the_test_suite(self) -> None:
        text = (ROOT / "tools" / "check.py").read_text(encoding="utf-8")
        self.assertIn("run_tests", text)
        self.assertIn("run_suite", text)

    def test_merge_commits_run_the_gate_too(self) -> None:
        hook = ROOT / ".githooks" / "pre-merge-commit"
        self.assertTrue(os.access(hook, os.X_OK), "hook is not executable")
        self.assertIn("tools.check --full", hook.read_text(encoding="utf-8"))

    def test_dev_tools_are_pinned(self) -> None:
        text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        for tool in ("ruff", "black", "pyright"):
            self.assertIn(f"{tool}==", text)


class HooksCheckTest(unittest.TestCase):
    """``hooks_installed`` accepts the tracked hooks folder in any form."""

    def configured(self, value: str) -> bool:
        """Run the check as if ``core.hooksPath`` held ``value``.

        Args:
            value: The setting; an empty string means it is unset.

        Returns:
            What ``hooks_installed`` answers.
        """
        # main_checkout also calls git_output, so pin it to this checkout.
        with (
            mock.patch.object(check, "git_output", return_value=value),
            mock.patch.object(check, "main_checkout", return_value=ROOT),
        ):
            return hooks_installed()

    def test_relative_and_absolute_forms_both_count(self) -> None:
        self.assertTrue(self.configured(".githooks"))
        self.assertTrue(self.configured(str(ROOT / ".githooks")))

    def test_unset_or_other_folders_do_not_count(self) -> None:
        self.assertFalse(self.configured(""))
        self.assertFalse(self.configured("hooks"))
        self.assertFalse(self.configured("/tmp/elsewhere/.githooks"))


if __name__ == "__main__":
    unittest.main()
