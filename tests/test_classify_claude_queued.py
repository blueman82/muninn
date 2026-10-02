"""Claude input queued while Claude works: only the owner's is stored."""

from __future__ import annotations

import unittest

from muninn import classify as c
from muninn import knowledge
from tests.classify_claude_support import (
    MAIN,
    SESSION,
    cev,
    claude_rec,
    queued_attachment,
    text_block,
)
from tests.ingest_support import IngestCase

OWNER_TEXT = "please also cover the zebra cache"
NOT_OWNER = ("task-notification", "peer", "coordinator", "auto-continuation")


class QueuedClassifyTests(unittest.TestCase):
    """What ``claude_events`` makes of queued_command attachments."""

    def test_owner_string_prompt_is_a_prompt(self) -> None:
        events = c.claude_events(queued_attachment(OWNER_TEXT), 7)
        self.assertEqual(events, [cev("prompt", OWNER_TEXT)])

    def test_owner_list_prompt_drops_images(self) -> None:
        blocks = [
            text_block(OWNER_TEXT),
            {"type": "image", "source": {"type": "base64", "data": "AA"}},
        ]
        events = c.claude_events(queued_attachment(blocks), 7)
        self.assertEqual(events, [cev("prompt", OWNER_TEXT)])
        image_only = queued_attachment(blocks[1:])
        self.assertEqual(c.claude_events(image_only, 7), [])

    def test_owner_sidechain_prompt_matches_other_sidechain_users(
        self,
    ) -> None:
        ordinary = claude_rec("user", OWNER_TEXT, sidechain=True)
        queued = queued_attachment(OWNER_TEXT, sidechain=True)
        self.assertEqual(
            c.claude_events(queued, 7), c.claude_events(ordinary, 7)
        )
        self.assertNotEqual(c.claude_events(queued, 7)[0].kind, "prompt")

    def test_other_origins_are_not_indexed(self) -> None:
        for origin in (*NOT_OWNER, None):
            with self.subTest(origin=origin):
                record = queued_attachment(OWNER_TEXT, origin=origin)
                self.assertEqual(c.claude_events(record, 7), [])

    def test_other_attachments_and_queue_operations_stay_out(self) -> None:
        other = queued_attachment(OWNER_TEXT)
        other["attachment"]["type"] = "hook_additional_context"
        queue_op = {"type": "queue-operation", "content": OWNER_TEXT}
        for record in (other, queue_op):
            self.assertEqual(c.claude_events(record, 7), [])

    def test_memory_marker_is_flagged_like_a_normal_prompt(self) -> None:
        text = '<muninn-memory source="x">pasted</muninn-memory>'
        queued = c.claude_events(queued_attachment(text), 7)
        ordinary = c.claude_events(claude_rec("user", text), 7)
        self.assertEqual(queued, ordinary)
        self.assertEqual(queued[0].flags & c.FLAG_MARKER, c.FLAG_MARKER)


class QueuedIngestTests(IngestCase):
    """Indexing, citing and re-reading a queued owner prompt."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.write(
            MAIN,
            [
                claude_rec("user", "earlier talk"),
                queued_attachment(OWNER_TEXT),
                queued_attachment("noise", origin="peer"),
            ],
            root="claude-projects",
        )

    def ref(self) -> str:
        """Return the ref of the queued prompt.

        Returns:
            The ``claude:`` reference of line 2.
        """
        return f"claude:{SESSION}:2.1"

    def test_quote_check_finds_the_owner_words(self) -> None:
        self.run_ingest()
        cite = knowledge.check_citation(self.conn, self.ref(), OWNER_TEXT)
        self.assertEqual(cite["quote"], OWNER_TEXT)
        self.assertEqual(
            [e[:3] for e in self.events(SESSION)][1], (2, 1, "prompt")
        )
        self.assertEqual(len(self.events(SESSION)), 2)

    def test_old_classifier_version_is_reread_without_duplicates(self) -> None:
        self.run_ingest()
        self.conn.execute(
            "UPDATE source SET classifier_version = ?",
            (c.CLASSIFIER_VERSION - 1,),
        )
        self.conn.execute("DELETE FROM event WHERE line = 2")
        self.conn.commit()
        self.run_ingest()
        self.assertEqual(len(self.events(SESSION)), 2)
        self.run_ingest()
        self.assertEqual(len(self.events(SESSION)), 2)
