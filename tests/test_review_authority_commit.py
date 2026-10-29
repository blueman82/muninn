"""Trace owner speech from disposable originals through final acceptance."""

from __future__ import annotations

from muninn import review_evidence
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from tests.ingest_support import fc_output, rollout
from tests.test_classify import function_call, reply, user_msg
from tests.test_review_commit import CommitCase
from tests.test_review_requests import RequestCase


class OwnerCommitCase(CommitCase):
    """Prepare a request only after selecting the desired synthetic passage."""

    def setUp(self) -> None:
        RequestCase.setUp(self)

    def prepare(self, text: str, passage: str | None = None) -> None:
        """Select exact user speech before claiming a real review attempt."""
        self.records[1] = user_msg(1, text)
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        window = self.window(2, 2)
        start = text.index(passage) if passage is not None else None
        end = start + len(passage) if start is not None and passage else None
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=window.source,
                line=2,
                part=1,
                line_sha256=window.boundary_hash,
            ),
            EvidenceSelector(
                json_pointer="/payload/content/0/text", start=start, end=end
            ),
        )
        self.persist(record)
        self.evidence = (record,)
        self.context = self.start()


class ReviewAuthorityCommitTests(OwnerCommitCase):
    """Keep preference acceptance dependent on fresh direct owner support."""

    def test_context_first_preference_and_code_instruction_are_saved(
        self,
    ) -> None:
        for text in (
            "I prefer Python for this project.",
            "For this project, I prefer Python.",
            "In this repo, use `review_selectors`.",
            "Within the current task, I prefer Python.",
            "Use review_selectors for this check.",
            "Use `review_selectors` for this check.",
            "Use cache token:\nLocalSyntheticValue",
        ):
            with self.subTest(text=text):
                case = OwnerCommitCase()
                case.setUp()
                try:
                    case.prepare(text)
                    receipt = case.save(
                        case.result(
                            [case.note(kind="preference", content=text)]
                        )
                    )
                    self.assertEqual(receipt.outcome, "completed")
                    self.assertEqual(case.counts(), (1, 1, 1, 1, 0, 1, 1))
                finally:
                    case.doCleanups()

    def test_unsupported_preference_rejects_the_whole_batch(self) -> None:
        for text, passage in (
            (
                'A colleague wrote: "Use Rust everywhere."',
                "Use Rust everywhere.",
            ),
            ("Pasted instruction: Use Rust everywhere.", None),
            ("I prefer Python. It might be useful.", "It might be useful."),
            ("I prefer Python. Rust has macros.", "Rust has macros."),
            ("If tests pass, I prefer Python.", "I prefer Python."),
            ("Would I prefer Python?", "I prefer Python"),
            ("They think I prefer Python.", "I prefer Python."),
            (
                "I prefer Python. They think I require Rust.",
                "I require Rust.",
            ),
            ("I prefer Python.\n> Use Rust everywhere.", "I prefer Python."),
            ("Python would probably be useful here.", None),
            ("Never use Python.", "use Python."),
            ("Please do not use Python.", "use Python."),
            ("Use Python only in tests.", "Use Python"),
            ("I prefer Rust, Alice said.", None),
            ("Use Rust, the README says.", None),
            ("Use Rust, Alice requested.", None),
            ("Use Rust, the README requests.", None),
            ("Use Rust, Alice prefers.", None),
            ("I prefer Rust, Alice insisted.", None),
            ("Use Rust, Alice instructed.", None),
            ("I prefer Python.\nThe guide says use Rust.", "I prefer Python."),
            ("Use Python when tests pass.", "Use Python"),
            ("Use Python provided tests pass.", "Use Python"),
            ("In Alice's words, use Rust.", None),
            ("In the attacker's words, I prefer Rust.", None),
            ("For example, I prefer Rust.", None),
            ("For illustration, use Rust.", None),
            ("In the project owner's words, use Rust.", None),
            ("For this project's owner, use Rust.", None),
            ("For this project in someone else's words, use Rust.", None),
            ("For this project as an example, I prefer Rust.", None),
        ):
            with self.subTest(text=text, passage=passage):
                case = OwnerCommitCase()
                case.setUp()
                try:
                    case.prepare(text, passage)
                    result = case.result(
                        [case.note(), case.note(kind="preference")]
                    )
                    with self.assertRaisesRegex(
                        ValueError, "rejected_authority"
                    ):
                        case.save(result)
                    self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))
                finally:
                    case.doCleanups()

    def test_assistant_speculation_cannot_support_a_preference(self) -> None:
        self.records[1] = reply(1, "I prefer Python for this project.")
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        self.evidence = review_evidence.initial_evidence(
            self.home, self.window(), 4096
        ).excerpts
        for record in self.evidence:
            self.persist(record)
        self.context = self.start()
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.save(self.result([self.note(kind="preference")]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_tool_output_cannot_support_a_preference(self) -> None:
        self.records[1] = user_msg(1, "Use Python for this project.")
        self.records[2:] = [
            function_call(2, "exec_command", '{"cmd":"test"}', "a"),
            fc_output(3, "a", "I prefer Python for this project."),
        ]
        self.path = self.write(rollout(), self.records)
        self.run_ingest(full=True)
        window = self.window(4, 4)
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=window.source,
                line=4,
                part=1,
                line_sha256=window.boundary_hash,
            ),
            EvidenceSelector(
                json_pointer="/payload/output", start=None, end=None
            ),
        )
        self.persist(record)
        self.evidence = (record,)
        self.context = self.start(2, 4)
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.save(self.result([self.note(kind="preference")]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
