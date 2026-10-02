"""Claude input queued while Claude works: only the owner's is stored."""

from __future__ import annotations

import unittest

from muninn import classify as c
from muninn import knowledge
from muninn.claude_events import (
    CHANNEL_HUMAN_FLAG,
    CLAUDE_HARNESS_PREFIXES,
    META_FLAG,
    OWNER_ORIGIN_KIND,
    QUEUED_COMMAND_TYPE,
)
from tests.classify_claude_support import (
    MAIN,
    SESSION,
    cev,
    claude_rec,
    queued_attachment,
    text_block,
)
from tests.classify_support import GHP
from tests.ingest_support import IngestCase

OWNER_TEXT = "please also cover the zebra cache"
# The classifier version before queued owner input was indexed; sources
# stamped with it must be re-read.
BEFORE_QUEUED_INPUT = 1
NOT_OWNER = ("task-notification", "peer", "coordinator", "auto-continuation")


class QueuedClassifyTests(unittest.TestCase):
    """What ``claude_events`` makes of queued_command attachments."""

    def test_the_names_match_what_claude_code_writes(self) -> None:
        """The fixtures use the constants, so pin the real wire names here."""
        self.assertEqual(QUEUED_COMMAND_TYPE, "queued_command")
        self.assertEqual(OWNER_ORIGIN_KIND, "human")
        self.assertEqual(META_FLAG, "isMeta")

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

    def test_a_channel_participant_is_not_the_owner(self) -> None:
        record = queued_attachment(
            OWNER_TEXT, in_attachment={CHANNEL_HUMAN_FLAG: True}
        )
        self.assertEqual(c.claude_events(record, 7), [])

    def test_an_attachment_marked_meta_is_harness_text(self) -> None:
        record = queued_attachment(OWNER_TEXT, in_attachment={META_FLAG: True})
        (event,) = c.claude_events(record, 7)
        self.assertEqual(event.kind, "harness")

    def test_only_a_true_meta_flag_makes_harness_text(self) -> None:
        unset = queued_attachment(OWNER_TEXT, in_attachment={META_FLAG: False})
        self.assertEqual(
            c.claude_events(unset, 7), [cev("prompt", OWNER_TEXT)]
        )
        on_record = queued_attachment(OWNER_TEXT)
        on_record[META_FLAG] = True
        (event,) = c.claude_events(on_record, 7)
        self.assertEqual(event.kind, "harness")

    def test_a_harness_prefix_makes_harness_text_as_for_any_user(self) -> None:
        text = CLAUDE_HARNESS_PREFIXES[0] + " injected"
        queued = c.claude_events(queued_attachment(text), 7)
        ordinary = c.claude_events(claude_rec("user", text), 7)
        self.assertEqual(queued, ordinary)
        self.assertEqual(queued[0].kind, "harness")

    def test_a_secret_is_redacted_as_in_any_user_message(self) -> None:
        text = f"my token is {GHP}"
        queued = c.claude_events(queued_attachment(text), 7)
        ordinary = c.claude_events(claude_rec("user", text), 7)
        self.assertEqual(queued, ordinary)
        self.assertNotIn(GHP, queued[0].text)

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
        self.assertGreater(c.CLASSIFIER_VERSION, BEFORE_QUEUED_INPUT)
        self.conn.execute(
            "UPDATE source SET classifier_version = ?",
            (BEFORE_QUEUED_INPUT,),
        )
        self.conn.execute("DELETE FROM event WHERE line = 2")
        self.conn.commit()
        self.run_ingest()
        self.assertEqual(len(self.events(SESSION)), 2)
        self.run_ingest()
        self.assertEqual(len(self.events(SESSION)), 2)
