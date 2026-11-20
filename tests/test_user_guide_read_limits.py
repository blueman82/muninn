"""Check the two read-limit entries of docs/USER-GUIDE.md.

The two entries that told the person to tell a maintainer are replaced.
All other entries keep their headings and their order.
"""

from __future__ import annotations

import unittest

from tests.test_user_guide import entries

OLD = (
    "Tell your maintainer about a read limit",
    "Tell your maintainer which read limit stopped a task",
)
BEFORE = [
    "Get help when an upgrade cannot finish",
    "Fix an unfinished Codex interface change",
    "Find out why a learning task stopped",
    "Find out what a learning notice means",
    "Fix a clock error",
    "Read a changed chat again",
    "Start a task again after a rule change",
    "Do nothing when a different worker took a task",
]
AFTER = [
    "Tell your maintainer about a size limit",
    "Start a stopped task again after a time-out",
    "Find a chat again",
    "Do nothing when Muninn rejects a result",
    "Do nothing when Muninn skips a task",
    "Do nothing when Codex changed during a task",
    "Sign in to Codex again",
    "Get help when setup has no known cause",
    "Get help when a learning setup file is not correct",
    "Get help when a launch file is not correct",
    "Get help when Codex or your computer differs from the approved setup",
    "Get help when Codex cannot start for learning",
    "Keep learning off after a safety stop",
    "Keep learning off when Muninn cannot check its policy",
    "Turn learning on again after a pause",
]
DETAILED = (
    "evidence_byte_limit",
    "evidence_record_limit",
    "evidence_work_limit",
    "evidence_selection_limit",
)


def new_entries() -> list[tuple[str, str]]:
    """Return the two entries between the unchanged headings.

    Returns:
        The (heading, body) pairs in guide order.
    """
    return entries()[len(BEFORE) : -len(AFTER)]


class ReadLimitEntryTest(unittest.TestCase):
    """Checks of the two replaced read-limit entries."""

    def test_old_headings_are_gone(self) -> None:
        """The two maintainer headings are no longer in the guide."""
        headings = [heading for heading, _ in entries()]
        for old in OLD:
            with self.subTest(old=old):
                self.assertNotIn(old, headings)

    def test_other_entries_keep_their_place(self) -> None:
        """Only the two read-limit entries change place or heading."""
        headings = [heading for heading, _ in entries()]
        self.assertEqual(len(headings), len(BEFORE) + 2 + len(AFTER))
        self.assertEqual(headings[: len(BEFORE)], BEFORE)
        self.assertEqual(headings[-len(AFTER) :], AFTER)

    def test_new_headings_are_unique(self) -> None:
        """Each new heading differs from all other headings."""
        headings = [heading for heading, _ in entries()]
        for heading, _ in new_entries():
            with self.subTest(heading=heading):
                self.assertEqual(headings.count(heading), 1)

    def test_entries_name_the_codes(self) -> None:
        """The first entry names the generic code, the second the four."""
        first, second = new_entries()
        self.assertTrue(first[1].strip().startswith("`evidence_limit`"))
        for code in DETAILED:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", second[1])

    def test_entries_do_not_send_the_person_to_a_maintainer(self) -> None:
        """A read limit does not reach the person as a maintainer task."""
        for heading, body in new_entries():
            with self.subTest(heading=heading):
                self.assertNotIn("maintainer", f"{heading}\n{body}".lower())


if __name__ == "__main__":
    unittest.main()
