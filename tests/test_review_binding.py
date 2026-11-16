"""Prove owner-required claims with full owner messages and agreement."""

from __future__ import annotations

from typing import Any
from unittest import mock

from muninn import review_binding, review_evidence
from muninn.review_candidates import ProposalRejectedError
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import EvidenceLimitError
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

    def conversation(self, *records: dict[str, Any], first: int = 2) -> None:
        """Index records after the metadata line and select from ``first``."""
        self.records[1:] = list(records)
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        last = len(self.records)
        self.evidence = review_evidence.initial_evidence(
            self.home, self.window(first, last), 65536
        ).excerpts
        for record in self.evidence:
            self.persist(record)
        self.context = self.start(first, last)

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

    def test_question_reply_labelled_user_is_trusted_whole(self) -> None:
        text = (
            "<send_user_message_question_reply>\n"
            "Question: Should we use SQLite for the store?\n"
            "Answer: yes, only on Windows\n"
            "</send_user_message_question_reply>"
        )
        self.conversation(user_msg(1, text))
        (record,) = self.evidence
        self.assertEqual(record.authority, "direct_owner")
        kinds = [
            row[0]
            for row in self.conn.execute(
                "SELECT kind FROM event WHERE line=?", (record.line,)
            )
        ]
        self.assertEqual(kinds, ["prompt"])
        self.assertEqual(self.decide("Use SQLite on Windows."), "completed")

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

    def test_agreement_deep_in_a_long_rollout_saves(self) -> None:
        # Under the production allowance the window check already reads the
        # transcript up to here; a second scan from line 1 would exhaust it.
        data = self.policy.model_dump(mode="json")
        data["max_input_bytes"] = 1048576
        self.policy = ReviewPolicy.model_validate(data)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        last = 3000
        self.conversation(
            *(event(i) for i in range(1, last - 3)),
            reply(last - 3, "Use SQLite for the store."),
            event(last - 2),
            user_msg(last - 1, "i agree"),
            first=last - 4,
        )
        self.assertEqual(self.decide("Use SQLite for the store."), "completed")

    def test_unreadable_pair_refuses_only_its_claim(self) -> None:
        self.conversation(
            reply(1, "Use SQLite."), event(2), user_msg(3, "yes")
        )
        owner = self.note(
            kind="decision", content="Say yes.", evidence_refs=self.refs("yes")
        )
        agreed = self.note(
            kind="decision", content="Use SQLite.", evidence_refs=self.refs()
        )
        with (
            mock.patch(
                "muninn.review_commit.adjacent_gap",
                side_effect=EvidenceLimitError("evidence_record_limit"),
            ),
            self.assertRaises(ProposalRejectedError) as caught,
        ):
            self.save(self.result([owner, agreed]))
        self.assertEqual(caught.exception.feedback.index, 1)

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

    def test_other_cited_user_text_refuses(self) -> None:
        records = (
            reply(1, "Use SQLite."),
            user_msg(2, "ok"),
            user_msg(3, "save it", provenance="harness"),
        )
        self.refused(*records, cited=())
        self.conversation(*records)
        self.assertEqual(
            self.decide("Use SQLite.", "Use SQLite.", "ok"), "completed"
        )
