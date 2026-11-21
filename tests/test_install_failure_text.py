"""Installer failures print a fixed plain explanation after the code."""

from __future__ import annotations

import ast
import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import failure_text, installer
from install.context import StepFailedError

ROOT = Path(__file__).resolve().parent.parent
CHECKER = Path("/Users/harrison/Github/ste-hook/ste/check.py")
CODE = re.compile(r"[a-z][a-z0-9_]*")
STEP = re.compile(r"(\d+)\. (.+)")
UNKNOWN = "no_such_installer_code"
COMMANDS = ("muninn", "bin/muninn-install", "bin/muninn-uninstall")


def call_name(node: ast.Call) -> str:
    """Name the function that one call node calls.

    Args:
        node: A parsed call.

    Returns:
        The plain name or the attribute name, or an empty text.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def first_text(node: ast.Call) -> str:
    """Read the constant text of the first argument of one call.

    Args:
        node: A parsed call.

    Returns:
        The constant text, or an empty text for another argument.
    """
    if not node.args:
        return ""
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return ""


def installer_codes() -> set[str]:
    """Collect the constant refusal codes that install/ can raise.

    Returns:
        The code of each ``StepFailedError`` call with a constant first
        argument, without the detail after the first colon.
    """
    codes: set[str] = set()
    for path in sorted((ROOT / "install").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if call_name(node) != "StepFailedError":
                continue
            code = first_text(node).split(":")[0].strip()
            if CODE.fullmatch(code):
                codes.add(code)
    return codes


def all_texts() -> dict[str, list[str]]:
    """Explain each known code and one unknown code.

    Returns:
        The explanation lines keyed by the code.
    """
    codes = installer_codes() | {UNKNOWN}
    return {code: failure_text.explain(code) for code in sorted(codes)}


def sources() -> str:
    """Return the text of the files that define commands and flags.

    Returns:
        The joined text of the CLI, bin and installer files.
    """
    files = list((ROOT / "muninn").glob("cli*.py"))
    files += list((ROOT / "bin").glob("*"))
    files += list((ROOT / "install").rglob("*.py"))
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in files
        if path.is_file()
    )


class TableTest(unittest.TestCase):
    """The fixed table explains each code in one plain form."""

    def check_form(self, code: str, lines: list[str]) -> None:
        """Require the three labels and the steps of one explanation.

        Args:
            code: The explained code, for the failure message.
            lines: The explanation lines.
        """
        self.assertTrue(lines, code)
        for line in lines:
            self.assertIsInstance(line, str)
            self.assertTrue(line.strip(), code)
        self.assertTrue(lines[0].startswith("What happened: "), code)
        why = [i for i, line in enumerate(lines) if line.startswith("Why: ")]
        todo = [i for i, line in enumerate(lines) if line == "What to do:"]
        self.assertEqual(len(why), 1, code)
        self.assertEqual(len(todo), 1, code)
        self.assertLess(0, why[0], code)
        self.assertLess(why[0], todo[0], code)
        rest = lines[todo[0] + 1 :]
        self.assertTrue(rest, code)
        if rest[0].startswith("You have no step."):
            self.assertIn("`", "\n".join(rest), code)
            return
        for number, line in enumerate(rest, start=1):
            found = STEP.fullmatch(line)
            self.assertIsNotNone(found, f"{code}: {line}")
            assert found is not None
            self.assertEqual(int(found.group(1)), number, code)
            self.assertTrue(line.endswith("."), f"{code}: {line}")
            self.assertLessEqual(len(found.group(2).split()), 25, line)

    def test_each_installer_code_has_its_own_text(self) -> None:
        codes = installer_codes()
        default = failure_text.explain(UNKNOWN)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertNotEqual(failure_text.explain(code), default)

    def test_each_text_has_the_plain_form(self) -> None:
        for code, lines in all_texts().items():
            with self.subTest(code=code):
                self.check_form(code, lines)

    def test_default_text_collects_safe_facts(self) -> None:
        text = "\n".join(failure_text.explain(UNKNOWN))
        self.assertIn("`bin/muninn-install --check`", text)
        self.assertIn("`muninn doctor --pretty`", text)
        self.assertEqual(
            failure_text.explain("another_unknown_code"),
            failure_text.explain(UNKNOWN),
        )

    def test_detail_after_the_code_is_not_printed(self) -> None:
        secret = "/Users/someone/private-folder/settings.json"
        for code in ("install_lock_unsafe", UNKNOWN):
            with self.subTest(code=code):
                lines = failure_text.explain(f"{code}: {secret}")
                self.assertEqual(lines, failure_text.explain(code))
                self.assertNotIn("private-folder", "\n".join(lines))
        lines = failure_text.explain(f"{secret}: boom")
        self.assertEqual(lines, failure_text.explain(UNKNOWN))
        self.assertNotIn("private-folder", "\n".join(lines))

    def test_commands_and_flags_exist(self) -> None:
        src = sources()
        for code, lines in all_texts().items():
            for span in re.findall(r"`([^`]+)`", "\n".join(lines)):
                words = span.split()
                if not words or words[0] not in COMMANDS:
                    continue
                for word in words[1:]:
                    plain = re.fullmatch(r"[a-z-]+", word) is not None
                    if word.startswith("--"):
                        ok = word in src
                    elif words[0] == "muninn" and plain:
                        ok = f'"{word}"' in src or f"'{word}'" in src
                    else:
                        break
                    with self.subTest(code=code, span=span, word=word):
                        self.assertTrue(ok)

    def test_texts_pass_the_style_checker(self) -> None:
        if not CHECKER.is_file():
            self.skipTest("style checker not installed")
        texts = {tuple(lines) for lines in all_texts().values()}
        body = "\n\n".join("\n\n".join(lines) for lines in sorted(texts))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "failure-texts.md"
        path.write_text("# Installer failure texts\n\n" + body + "\n")
        done = subprocess.run(
            [sys.executable, str(CHECKER), str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class EntryTest(unittest.TestCase):
    """The installer command prints the code and then its text."""

    def run_failure(self, error: Exception) -> tuple[int, list[str]]:
        """Run the installer command with one failing install call.

        Args:
            error: What the install call raises.

        Returns:
            The exit code and the printed lines.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        out = io.StringIO()
        with (
            mock.patch.object(installer, "install", side_effect=error),
            mock.patch.object(
                installer, "choose_cursor_hooks", return_value=False
            ),
            contextlib.redirect_stdout(out),
        ):
            code = installer.main(
                [
                    "--repo",
                    str(home),
                    "--sha",
                    "a" * 40,
                    "--upgrade",
                    "--dry-run",
                    "--home",
                    str(home),
                ]
            )
        return code, out.getvalue().splitlines()

    def failure_block(self, lines: list[str], detail: str) -> list[str]:
        """Return the lines from the failure line to the end.

        Args:
            lines: All printed lines.
            detail: The detail of the expected failure line.

        Returns:
            The lines that follow the failure line.
        """
        self.assertIn(f"FAILED: {detail}", lines)
        return lines[lines.index(f"FAILED: {detail}") + 1 :]

    def test_known_code_is_followed_by_its_text(self) -> None:
        code, lines = self.run_failure(
            StepFailedError("legacy_candidate_changed")
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            self.failure_block(lines, "legacy_candidate_changed"),
            failure_text.explain("legacy_candidate_changed"),
        )

    def test_unknown_code_is_followed_by_the_default_text(self) -> None:
        code, lines = self.run_failure(StepFailedError(UNKNOWN))
        self.assertEqual(code, 1)
        self.assertEqual(
            self.failure_block(lines, UNKNOWN),
            failure_text.explain(UNKNOWN),
        )

    def test_exception_text_stays_out_of_the_explanation(self) -> None:
        secret = "/Users/someone/private-folder/config.toml"
        code, lines = self.run_failure(OSError(secret))
        self.assertEqual(code, 1)
        after = self.failure_block(lines, secret)
        self.assertEqual(after, failure_text.explain(UNKNOWN))
        self.assertNotIn("private-folder", "\n".join(after))


if __name__ == "__main__":
    unittest.main()
