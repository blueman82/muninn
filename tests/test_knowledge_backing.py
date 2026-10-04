"""What backs an entry's text, and that the block and the prompt agree."""

from __future__ import annotations

import unittest

from muninn import knowledge
from muninn.knowledge_model import PUSH_OVERLAP, text_backed
from tests.knowledge_support import KnowCase, kid


class TextBackedTests(unittest.TestCase):
    """The pure rule."""

    def test_the_overlap_boundary_is_inclusive(self) -> None:
        self.assertEqual(PUSH_OVERLAP, 0.8)
        quote = "alpha bravo charlie delta"
        self.assertTrue(text_backed("alpha bravo charlie delta echo", quote))
        self.assertFalse(
            text_backed("alpha bravo charlie echo foxtrot", quote)
        )

    def test_case_and_spacing_are_folded(self) -> None:
        self.assertTrue(text_backed("Use  THE\nZebra", "use the zebra cache"))
        self.assertTrue(
            text_backed("use the zebra", "We said:  USE\tthe\n zebra today")
        )

    def test_words_under_three_characters_do_not_count(self) -> None:
        # Only "cat" counts, and the quote holds it.
        self.assertTrue(text_backed("a cat is on it", "one cat only"))
        # No word of three characters: only a substring can back it.
        self.assertFalse(text_backed("go to it", "go and see to it"))
        self.assertTrue(text_backed("go to it", "so go to it now"))

    def test_empty_text_is_never_backed(self) -> None:
        self.assertFalse(text_backed("", "anything"))
        self.assertFalse(text_backed(" \n ", ""))

    def test_an_empty_quote_backs_nothing(self) -> None:
        self.assertFalse(text_backed("use the zebra cache", ""))


class BackingCiteTests(KnowCase):
    """Both readers apply one rule to the same rows."""

    def cite(self, event: int, quote: str) -> list[tuple[str, str]]:
        """Return one ``(ref, quote)`` pair."""
        return [(self.ref(event), quote)]

    def test_many_citations_of_one_entry_do_not_starve_the_rest(self) -> None:
        user = self.cite(self.prompt, "use the zebra cache")
        others = [
            kid(self.add(text=f"use the zebra cache {i}", cites=user))
            for i in range(3)
        ]
        hog = kid(self.add(text="use the zebra cache hog", cites=user))
        # The newest entry gets far more citation rows than the scan reads.
        sid = self.rw.execute(
            "SELECT scope_id FROM knowledge WHERE id = ?", (hog,)
        ).fetchone()[0]
        self.assertEqual(sid, self.repo)
        for _ in range(60):
            self.rw.execute(
                "INSERT INTO citation(knowledge_id, provider, thread_id,"
                " line, part, line_sha256, role, kind, quote, span_start,"
                " span_end) SELECT knowledge_id, provider, thread_id, line,"
                " part, line_sha256, role, kind, quote, span_start,"
                " span_end FROM citation WHERE knowledge_id = ? LIMIT 1",
                (hog,),
            )
        got = knowledge.block_entries(self.ro(), [self.repo], limit=3)
        self.assertEqual(
            [e["id"] for e in got],
            [f"K{hog}", f"K{others[-1]}", f"K{others[-2]}"],
        )

    def test_the_block_and_the_prompt_rule_agree(self) -> None:
        good = kid(
            self.add(
                text="use the zebra cache",
                cites=self.cite(self.prompt, "use the zebra cache"),
            )
        )
        reply = kid(
            self.add(
                text="wire the zebra cache",
                cites=self.cite(self.reply, "wire the zebra cache"),
            )
        )
        overlap = kid(
            self.add(
                text="use the zebra cache always",
                cites=self.cite(self.prompt, "use the zebra cache"),
            )
        )
        unbacked = kid(
            self.add(
                text="use the zebra cache",
                cites=self.cite(self.prompt, "use the zebra"),
            )
        )
        self.rw.execute(
            "UPDATE knowledge SET text = 'zebra stripes everywhere matter'"
            " WHERE id = ?",
            (unbacked,),
        )
        blank = kid(
            self.add(
                text="use the zebra cache",
                cites=self.cite(self.prompt, "use the zebra cache"),
            )
        )
        self.rw.execute(
            "UPDATE citation SET quote = NULL WHERE knowledge_id = ?", (blank,)
        )
        ids = [good, reply, overlap, unbacked, blank]
        pushed = {
            int(e["id"][1:])
            for e in knowledge.block_entries(self.ro(), [self.repo], limit=50)
        }
        self.assertEqual(pushed, knowledge.user_cited(self.ro(), ids))
        self.assertEqual(pushed, {good, overlap})


if __name__ == "__main__":
    unittest.main()
