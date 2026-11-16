"""Prove owner-required claims with full owner messages and agreement."""

from __future__ import annotations

from typing import Any
from unittest import mock

from muninn import review_binding, review_evidence
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from tests.classify_support import item
from tests.ingest_support import fc_output, rollout
from tests.test_classify import function_call, reply, user_msg
from tests.test_review_commit import CommitCase
from tests.test_review_requests import RequestCase


def event(ordinal: int) -> dict[str, Any]:
    """Build a provider event line that projects no permitted field."""
    return item({"type": "token_count", "info": None}, ordinal, "event_msg")


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

    def refs(self, *cited: str) -> list[str]:
        """Return the ids of the named excerpts, or of all of them."""
        return [e.id for e in self.evidence if not cited or e.excerpt in cited]

    def decide(self, content: str, *cited: str) -> str:
        """Save one decision citing the named excerpts, or all of them."""
        note = self.note(
            kind="decision", content=content, evidence_refs=self.refs(*cited)
        )
        return self.save(self.result([note])).outcome

    def refused(
        self, *records: dict[str, Any], cited: tuple[str, ...]
    ) -> None:
        """Require a refusal for a decision on a new conversation."""
        case = BindingCase()
        case.setUp()
        try:
            case.conversation(*records)
            with self.assertRaisesRegex(ValueError, "rejected_authority"):
                case.decide("Use SQLite for the store.", *cited)
            self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))
        finally:
            case.doCleanups()


class OwnerMessageTests(BindingCase):
    """Accept any model text when a full owner message is cited."""

    def test_plain_owner_message_saves_model_text(self) -> None:
        self.conversation(user_msg(1, "we'll use pytest - and no mocks"))
        self.assertEqual(
            self.decide("Use pytest for tests and avoid mocks."), "completed"
        )

    def test_partial_span_gives_no_owner_support(self) -> None:
        self.conversation(user_msg(1, "Never use pytest. Use Python."))
        window = self.window(2, 2)
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=window.source,
                line=2,
                part=1,
                line_sha256=window.boundary_hash,
            ),
            EvidenceSelector(
                json_pointer="/payload/content/0/text", start=6, end=17
            ),
        )
        self.assertEqual(record.authority, "untrusted")

    def test_hook_message_is_not_owner_support(self) -> None:
        self.refused(
            user_msg(1, '<hook_prompt id="h">yes</hook_prompt>'), cited=()
        )


class AgreementTests(BindingCase):
    """Accept an agreement reply with the assistant reply directly before."""

    def test_agreement_after_assistant_reply_saves(self) -> None:
        self.conversation(
            reply(1, "I checked the options. Use SQLite for the store."),
            *(event(i) for i in range(2, 8)),
            user_msg(8, "i agree"),
        )
        self.assertEqual(self.decide("Use SQLite for the store."), "completed")

    def test_question_and_yes_save(self) -> None:
        self.conversation(
            reply(1, "Should we use SQLite for the store?"),
            user_msg(2, "yes"),
        )
        self.assertEqual(self.decide("Use SQLite for the store."), "completed")

    def test_gap_tool_call_or_message_refuses(self) -> None:
        for between in (
            [
                function_call(2, "exec_command", '{"cmd":"x"}', "a"),
                fc_output(3, "a", "done"),
            ],
            [reply(2, "Use Postgres instead.")],
        ):
            with self.subTest(between=between):
                self.refused(
                    reply(1, "Use SQLite."),
                    *between,
                    user_msg(9, "yes"),
                    cited=("Use SQLite.", "yes"),
                )

    def test_reply_after_owner_or_alone_refuses(self) -> None:
        self.refused(user_msg(1, "yes"), reply(2, "Use SQLite."), cited=())
        self.refused(
            reply(1, "Use SQLite."), user_msg(2, "yes"), cited=("Use SQLite.",)
        )

    def test_one_adjacency_read_per_pair(self) -> None:
        self.conversation(
            reply(1, "Use SQLite. Keep it local."), user_msg(2, "yes")
        )
        notes = [
            self.note(kind="decision", content=text, evidence_refs=self.refs())
            for text in ("Use SQLite.", "Keep the store local.")
        ]
        with mock.patch(
            "muninn.review_commit.adjacent_gap",
            wraps=review_binding.adjacent_gap,
        ) as gap:
            self.assertEqual(
                self.save(self.result(notes)).outcome, "completed"
            )
        self.assertEqual(gap.call_count, 1)
