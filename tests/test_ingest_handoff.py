"""Ingesting a Codex ChatGPT-handoff session: migration and citation."""

from __future__ import annotations

from muninn import classify as c
from muninn import knowledge
from muninn.codex_events import CHATGPT_HANDOFF, CHATGPT_REFERENCE_TAG
from muninn.knowledge_model import RefusedError
from tests.classify_support import codex_meta, user_msg
from tests.ingest_support import IngestCase, rollout

THREAD = "thr-h"
PASTED_WORDS = "CV Fit Assessment"
OWNER_TEXT = "please apply only to remote roles"
# The classifier version before handoff sessions were indexed.
BEFORE_HANDOFF = 2


class HandoffIngestTests(IngestCase):
    """A handoff session on disk, read by the real ingest."""

    def setUp(self) -> None:
        super().setUp()
        pasted = f'\n{CHATGPT_REFERENCE_TAG}:\n{{"title":"{PASTED_WORDS}"}}'
        self.write(
            rollout(THREAD),
            [
                codex_meta(CHATGPT_HANDOFF, THREAD),
                user_msg(1, pasted),
                user_msg(2, OWNER_TEXT),
            ],
        )

    def ref_of(self, kind: str) -> str:
        """Return the ref of the one event of ``kind``.

        Args:
            kind: The event kind to look for.

        Returns:
            Its ``codex:<thread>:<line>.<part>`` reference.
        """
        (row,) = self.conn.execute(
            "SELECT line, part FROM event WHERE kind = ?", (kind,)
        )
        return f"codex:{THREAD}:{row[0]}.{row[1]}"

    def test_an_older_other_row_gains_its_events_on_the_next_pass(
        self,
    ) -> None:
        self.run_ingest()
        # What the previous release left behind: a row, no events.
        self.conn.execute(
            "UPDATE source SET classifier_version = ?, thread_class = 'other',"
            " cursor_bytes = 0",
            (BEFORE_HANDOFF,),
        )
        self.conn.execute("DELETE FROM event")
        self.conn.commit()
        self.assertGreater(c.CLASSIFIER_VERSION, BEFORE_HANDOFF)
        self.run_ingest()
        kinds = self.conn.execute("SELECT kind FROM event ORDER BY line")
        self.assertEqual([k[0] for k in kinds], ["harness", "prompt"])
        self.run_ingest()
        count = self.conn.execute("SELECT count(*) FROM event").fetchone()[0]
        self.assertEqual(count, 2)

    def test_the_pasted_conversation_is_not_citable_but_the_owner_is(
        self,
    ) -> None:
        self.run_ingest()
        with self.assertRaises(RefusedError):
            knowledge.check_citation(
                self.conn, self.ref_of("harness"), PASTED_WORDS
            )
        cite = knowledge.check_citation(
            self.conn, self.ref_of("prompt"), OWNER_TEXT
        )
        self.assertEqual(cite["quote"], OWNER_TEXT)
