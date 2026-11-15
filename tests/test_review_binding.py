"""Bind owner-required claims to verbatim words and adjacent agreement."""

from __future__ import annotations

import unittest
from typing import Any

from muninn import knowledge_push, knowledge_read, review_evidence
from muninn.review_binding import verbatim
from muninn.review_json import canonical_json
from tests.classify_support import item
from tests.ingest_support import fc_output, rollout
from tests.test_classify import function_call, reply, user_msg
from tests.test_review_commit import CommitCase
from tests.test_review_requests import RequestCase


def event(ordinal: int) -> dict[str, Any]:
    """Build a provider event line that projects no permitted field."""
    return item({"type": "token_count", "info": None}, ordinal, "event_msg")


class VerbatimTests(unittest.TestCase):
    """Compare claim lines with whole sentences only."""

    def test_whole_sentence_runs_match(self) -> None:
        text = "I agree. Use SQLite for the store. Never use pytest."
        self.assertTrue(verbatim("Use SQLite for the store", text))
        self.assertTrue(verbatim("Use SQLite for the store.", text))
        self.assertTrue(verbatim("I agree.  Use SQLite\nfor the store.", text))
        self.assertTrue(verbatim("Never use pytest.", text))

    def test_partial_or_changed_words_do_not_match(self) -> None:
        text = "Never use pytest. Use SQLite for the store."
        for claim in (
            "use pytest",
            "use pytest.",
            "Use Postgres for the store.",
            "use SQLite for the store.",
            "Use SQLite",
            "Never use pytest. Use SQLite",
        ):
            with self.subTest(claim=claim):
                self.assertFalse(verbatim(claim, text))

    def test_question_is_not_a_sentence_boundary(self) -> None:
        text = "Should we use SQLite? It is local."
        self.assertTrue(verbatim("Should we use SQLite? It is local.", text))
        self.assertFalse(verbatim("It is local.", text))


class BindingCase(CommitCase):
    """Commit decisions against a disposable conversation."""

    def setUp(self) -> None:
        RequestCase.setUp(self)

    def conversation(self, *records: dict[str, Any]) -> None:
        """Index records after the metadata line and select all of them."""
        self.records[1:] = list(records)
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        last = len(self.records)
        self.evidence = review_evidence.initial_evidence(
            self.home, self.window(2, last), 65536
        ).excerpts
        for record in self.evidence:
            self.persist(record)
        self.context = self.start(2, last)

    def decide(self, content: str, *cited: str) -> str:
        """Save one decision citing the named excerpts, or all of them."""
        refs = [e.id for e in self.evidence if not cited or e.excerpt in cited]
        note = self.note(kind="decision", content=content, evidence_refs=refs)
        return self.save(self.result([note])).outcome


class OwnerLineBindingTests(BindingCase):
    """Bind decisions to sentences the owner typed."""

    def test_plain_owner_sentence_saves(self) -> None:
        self.conversation(user_msg(1, "we'll use pytest for tests"))
        self.assertEqual(
            self.decide("we'll use pytest for tests"), "completed"
        )

    def test_one_sentence_of_a_longer_owner_line_saves(self) -> None:
        self.conversation(user_msg(1, "I agree. Use SQLite for the store."))
        self.assertEqual(self.decide("Use SQLite for the store."), "completed")

    def test_other_words_are_refused(self) -> None:
        for text, claim in (
            ("Use pytest.", "Use Postgres."),
            ("Never use pytest.", "use pytest"),
        ):
            with self.subTest(claim=claim):
                case = BindingCase()
                case.setUp()
                try:
                    case.conversation(user_msg(1, text))
                    with self.assertRaisesRegex(
                        ValueError, "rejected_authority"
                    ):
                        case.decide(claim)
                    self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))
                finally:
                    case.doCleanups()

    def test_hook_yes_is_not_owner_support(self) -> None:
        self.conversation(user_msg(1, '<hook_prompt id="h">yes</hook_prompt>'))
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.decide('<hook_prompt id="h">yes</hook_prompt>')


class AgreementBindingTests(BindingCase):
    """Bind an agreement reply to the assistant reply directly before it."""

    def test_agreement_after_assistant_reply_saves(self) -> None:
        self.conversation(
            reply(1, "I checked the options. Use SQLite for the store."),
            *(event(i) for i in range(2, 8)),
            user_msg(8, "i agree"),
        )
        self.assertEqual(self.decide("Use SQLite for the store."), "completed")

    def test_question_and_yes_save_with_corrected_copy(self) -> None:
        self.conversation(
            reply(1, "Should we use SQLite for the store?"),
            user_msg(2, "yes"),
        )
        note = self.note(
            kind="decision",
            content="Should we use SQLite for the store?",
            evidence_refs=[e.id for e in self.evidence],
            corrected="Use SQLite for the store.",
        )
        self.assertEqual(self.save(self.result([note])).outcome, "completed")
        stored = self.row("SELECT content_json FROM knowledge_version")[0]
        self.assertIn('"corrected":"Use SQLite for the store."', stored)
        kid = self.row("SELECT id FROM knowledge")[0]
        owner_views = [
            knowledge_read.show_knowledge(self.conn, kid, 1),
            *knowledge_read.current_knowledge(self.conn, self.scope, 20.0),
        ]
        agent_views = [
            *knowledge_push.knowledge_context(
                self.conn, self.scope, 12000, 20.0
            ).notes,
            *knowledge_read.search_knowledge(self.conn, "SQLite", self.scope),
        ]
        self.assertEqual(len(agent_views), 2)
        for view in owner_views:
            self.assertIn("Use SQLite for the store.", canonical_json(view))
        for view in agent_views:
            self.assertNotIn("corrected", canonical_json(view))

    def test_gap_tool_call_or_message_refuses(self) -> None:
        for between in (
            [
                function_call(2, "exec_command", '{"cmd":"x"}', "a"),
                fc_output(3, "a", "done"),
            ],
            [user_msg(2, "wait")],
            [reply(2, "Use Postgres instead.")],
        ):
            with self.subTest(between=between):
                case = BindingCase()
                case.setUp()
                try:
                    case.conversation(
                        reply(1, "Use SQLite for the store."),
                        *between,
                        user_msg(9, "yes"),
                    )
                    with self.assertRaisesRegex(
                        ValueError, "rejected_authority"
                    ):
                        case.decide(
                            "Use SQLite for the store.",
                            "Use SQLite for the store.",
                            "yes",
                        )
                finally:
                    case.doCleanups()

    def test_reply_after_owner_refuses(self) -> None:
        self.conversation(
            user_msg(1, "yes"),
            reply(2, "Use SQLite for the store."),
        )
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.decide("Use SQLite for the store.")

    def test_assistant_reply_alone_refuses(self) -> None:
        self.conversation(
            reply(1, "Use SQLite for the store."),
            user_msg(2, "yes"),
        )
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.decide(
                "Use SQLite for the store.", "Use SQLite for the store."
            )
