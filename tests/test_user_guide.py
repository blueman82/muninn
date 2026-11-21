"""Checks for docs/USER-GUIDE.md and its README link."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "USER-GUIDE.md"
README = ROOT / "README.md"
CHECKER = Path("/Users/harrison/Github/ste-hook/ste/check.py")
LABELS = (
    "What you see:",
    "What caused it:",
    "What happens if you do nothing:",
    "How to repair it:",
    "Steps:",
    "Result:",
)
VERBS = {
    "Get",
    "Find",
    "Fix",
    "Read",
    "Start",
    "Do",
    "Tell",
    "Sign",
    "Keep",
    "Turn",
    "Check",
    "See",
    "Upgrade",
}
BANNED = ("ADR", "please", "click", "For more information")
LINK = "(docs/USER-GUIDE.md)"


def guide_text() -> str:
    """Return the text of the user guide.

    Returns:
        The full text of docs/USER-GUIDE.md.
    """
    return GUIDE.read_text(encoding="utf-8")


def fix_section() -> str:
    """Return the "Fix a problem" section of the guide.

    Returns:
        The guide text from the "Fix a problem" heading to the end.
    """
    text = guide_text()
    start = text.index("\n## Fix a problem")
    return text[start:]


def entries() -> list[tuple[str, str]]:
    """Return (heading, body) for each `###` entry of Fix a problem."""
    parts = re.split(r"^### (.+)$", fix_section(), flags=re.M)
    return list(zip(parts[1::2], parts[2::2], strict=True))


def code_entries() -> list[tuple[str, str]]:
    """Return the entries that describe a code.

    Returns:
        The (heading, body) pairs that have a "What you see" line.
    """
    return [(h, b) for h, b in entries() if "**What you see:**" in b]


class GuideFileTest(unittest.TestCase):
    """Checks of the guide file as a whole."""

    def test_guide_exists_with_title(self) -> None:
        self.assertTrue(GUIDE.is_file())
        self.assertTrue(guide_text().startswith("# Muninn user guide"))

    def test_task_sections_exist(self) -> None:
        text = guide_text()
        for title in (
            "Check that Muninn works",
            "See your memories",
            "Turn learning on",
            "Upgrade Muninn",
            "Fix a problem",
        ):
            with self.subTest(title=title):
                self.assertIn(f"\n## {title}\n", text)

    def test_banned_words_absent(self) -> None:
        text = guide_text()
        for word in BANNED:
            with self.subTest(word=word):
                self.assertNotIn(word.lower(), text.lower())

    def test_no_semicolons(self) -> None:
        self.assertNotIn(";", guide_text())


class EntryTest(unittest.TestCase):
    """Checks of each entry in Fix a problem."""

    def test_required_codes_have_entries(self) -> None:
        text = fix_section()
        for code in (
            "budget_clock_reversed",
            "incomplete_window",
            "review_policy_required",
            "rejected_fenced",
            "evidence_limit",
            "prompt_limit",
            "source_unavailable",
            "rejected_authority",
            "runtime_race_exhausted",
            "auth",
            "config",
            "paused",
            "native_tool_event",
        ):
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", text)

    def test_headings_start_with_verb(self) -> None:
        for heading, _ in entries():
            with self.subTest(heading=heading):
                self.assertIn(heading.split()[0], VERBS)
                self.assertFalse(heading.endswith("?"))

    def test_code_line_follows_heading(self) -> None:
        for heading, body in code_entries():
            with self.subTest(heading=heading):
                first = body.strip().splitlines()[0]
                self.assertTrue(first.startswith("`"), first)

    def test_six_labels_in_order(self) -> None:
        for heading, body in code_entries():
            with self.subTest(heading=heading):
                last = -1
                for label in LABELS:
                    at = body.find(f"**{label}**")
                    self.assertGreater(at, last, label)
                    last = at

    def test_steps_numbered_or_none(self) -> None:
        for heading, body in code_entries():
            with self.subTest(heading=heading):
                steps = body.split("**Steps:**")[1].split("- **Result")[0]
                if steps.strip() == "none.":
                    continue
                self.assertRegex(steps, r"\n\s+1\. ")
                for line in re.findall(r"^\s+\d+\. (.+)$", steps, re.M):
                    self.assertTrue(line.endswith("."), line)
                    self.assertLessEqual(len(line.split()), 25, line)


class CommandTest(unittest.TestCase):
    """Checks that each command in the guide exists."""

    def sources(self) -> str:
        """Return the text of the files that define commands.

        Returns:
            The joined text of the CLI, bin and installer files.
        """
        files = list((ROOT / "muninn").glob("cli*.py"))
        files += list((ROOT / "bin").glob("*"))
        files += list((ROOT / "install").rglob("*.py"))
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in files
            if p.is_file()
        )

    def test_commands_and_flags_exist(self) -> None:
        src = self.sources()
        text = guide_text()
        spans = re.findall(r"`([^`]+)`", text)
        for span in spans:
            words = span.split()
            if not words or words[0] != "muninn":
                continue
            for word in words[1:]:
                if word.startswith("--"):
                    ok = word in src
                elif re.fullmatch(r"[a-z-]+", word):
                    ok = f'"{word}"' in src or f"'{word}'" in src
                else:
                    break
                with self.subTest(span=span, word=word):
                    self.assertTrue(ok)
        for flag in set(re.findall(r"muninn-install[^`]* (--[a-z-]+)", text)):
            with self.subTest(flag=flag):
                self.assertIn(flag, src)


class CheckerTest(unittest.TestCase):
    """Runs the style checker on the guide."""

    def test_style_checker_reports_no_violation(self) -> None:
        if not CHECKER.is_file():
            self.skipTest("style checker not installed")
        done = subprocess.run(
            [sys.executable, str(CHECKER), str(GUIDE)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class ReadmeTest(unittest.TestCase):
    """Checks the one changed line of README.md."""

    def test_link_ends_line_27(self) -> None:
        lines = README.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 150)
        self.assertTrue(lines[26].startswith("- Templates:"))
        self.assertIn(
            "See `docs/QUICKSTART.md`, `docs/REFERENCE.md`, "
            "`docs/TROUBLESHOOTING.md`, and the [user guide",
            lines[26],
        )
        self.assertIn("`launchd/com.muninn.plist`", lines[26])
        self.assertTrue(lines[26].endswith(f"{LINK}."), lines[26])
        self.assertEqual("\n".join(lines).count(LINK), 1)


if __name__ == "__main__":
    unittest.main()
