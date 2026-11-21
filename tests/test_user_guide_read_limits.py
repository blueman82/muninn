"""Check the two read-limit entries of docs/USER-GUIDE.md."""

from __future__ import annotations

import unittest

from tests.test_user_guide import entries

DETAILED = (
    "evidence_byte_limit",
    "evidence_record_limit",
    "evidence_work_limit",
    "evidence_selection_limit",
)


def read_limit_entries() -> list[tuple[str, str]]:
    """Return the two read-limit entries.

    Returns:
        The (heading, body) pairs in guide order.
    """
    return [
        (heading, body)
        for heading, body in entries()
        if "`evidence_" in body.strip().splitlines()[0]
    ]


class ReadLimitEntryTest(unittest.TestCase):
    """Checks of the two read-limit entries."""

    def test_each_entry_exists_once(self) -> None:
        """The guide has one generic and one detailed entry."""
        found = read_limit_entries()
        self.assertEqual(len(found), 2)
        headings = [heading for heading, _ in entries()]
        for heading, _ in found:
            with self.subTest(heading=heading):
                self.assertEqual(headings.count(heading), 1)

    def test_entries_name_the_codes(self) -> None:
        """The first entry names the generic code, the second the four."""
        first, second = read_limit_entries()
        self.assertTrue(first[1].strip().startswith("`evidence_limit`"))
        for code in DETAILED:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", second[1])

    def test_entries_do_not_send_the_person_to_a_maintainer(self) -> None:
        """A read limit does not reach the person as a maintainer task."""
        for heading, body in read_limit_entries():
            with self.subTest(heading=heading):
                self.assertNotIn("maintainer", f"{heading}\n{body}".lower())


if __name__ == "__main__":
    unittest.main()
