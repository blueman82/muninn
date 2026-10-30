"""Check deferred candidate refusals and exact ordered batch reductions."""

from __future__ import annotations

from copy import deepcopy
from unittest import mock

from muninn import review_commit, review_correction, review_evidence
from muninn.knowledge_models import ReviewResult
from muninn.review_candidates import ProposalRejectedError
from tests.test_review_commit import CommitCase


class CandidateReductionTests(CommitCase):
    """Keep original checks terminal and candidate rollback opt-in."""

    def candidate(self) -> dict[str, object]:
        """Build a preference lacking direct owner support."""
        return self.note(
            kind="preference", evidence_refs=[self.evidence[1].id]
        )

    def deferred(self, result: ReviewResult) -> None:
        """Exercise the optional correction boundary on the real database."""
        review_commit.commit_review(
            self.conn, self.context, result, defer_candidate_failure=True
        )

    def attempt(self) -> tuple[object, ...]:
        """Observe only the current attempt's durable completion state."""
        return tuple(
            self.row(
                "SELECT ended_at,error_code FROM review_attempt WHERE id=?",
                (self.context.attempt_id,),
            )
        )

    def test_default_failure_still_ends_attempt(self) -> None:
        with (
            mock.patch.object(
                review_commit, "_failure", wraps=review_commit._failure
            ) as failures,
            self.assertRaisesRegex(ValueError, "^rejected_authority$"),
        ):
            self.save(self.result([self.candidate()]))
        self.assertEqual(self.attempt(), (13.0, "rejected_authority"))
        self.assertEqual(failures.call_count, 1)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_deferred_failure_rolls_back_without_ending_attempt(self) -> None:
        with (
            mock.patch.object(
                review_commit,
                "validate_evidence",
                wraps=review_commit.validate_evidence,
            ) as originals,
            mock.patch.object(
                review_evidence, "reread", wraps=review_evidence.reread
            ) as reads,
            mock.patch.object(
                review_commit, "_failure", wraps=review_commit._failure
            ) as failures,
            self.assertRaisesRegex(
                ProposalRejectedError, "^rejected_authority$"
            ) as raised,
        ):
            self.deferred(self.result([self.note(), self.candidate()]))
        self.assertEqual(
            raised.exception.feedback.model_dump(),
            {
                "kind": "reduction",
                "code": "owner_support",
                "collection": "changes",
                "index": 1,
            },
        )
        self.assertEqual((originals.call_count, reads.call_count), (1, 2))
        self.assertEqual(failures.call_count, 0)
        self.assertEqual(self.attempt(), (None, None))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            self.save(self.result([self.note()])).outcome, "completed"
        )

    def test_original_checks_precede_candidate_deferral(self) -> None:
        self.path.write_bytes(
            self.path.read_bytes().replace(b"Python", b"Golang")
        )
        with self.assertRaisesRegex(
            ValueError, "^rejected_evidence$"
        ) as raised:
            self.deferred(self.result([self.candidate()]))
        self.assertFalse(hasattr(raised.exception, "feedback"))
        self.assertEqual(self.attempt(), (13.0, "rejected_evidence"))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_terminal_refs_win_in_either_order_and_same_candidate(
        self,
    ) -> None:
        for mode in ("first", "last", "same"):
            with self.subTest(mode=mode):
                case = CandidateReductionTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                bad = case.note(evidence_refs=["E_" + "f" * 32])
                eligible = case.note(
                    scope={"project_id": "other", "task": None}
                )
                changes = (
                    [bad, eligible] if mode == "first" else [eligible, bad]
                )
                if mode == "same":
                    changes = [
                        {**eligible, "evidence_refs": bad["evidence_refs"]}
                    ]
                with (
                    mock.patch.object(
                        review_commit, "_failure", wraps=review_commit._failure
                    ) as failures,
                    self.assertRaisesRegex(
                        ValueError, "^rejected_evidence$"
                    ) as raised,
                ):
                    case.deferred(case.result(changes))
                self.assertFalse(hasattr(raised.exception, "feedback"))
                self.assertEqual(failures.call_count, 1)
                self.assertEqual(case.attempt(), (13.0, "rejected_evidence"))
                self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))

    def target_batch(self) -> tuple[dict[str, object], dict[str, object]]:
        """Create a retained target and admit support in a new request."""
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(2, 2)
        replacement = self.note(
            action="replace",
            knowledge_id=saved.knowledge_id,
            expected_revision=1,
        )
        unresolved = {
            "claim": "Unknown",
            "affected": [saved.model_dump()],
            "evidence_refs": [],
            "question": "Which evidence is missing?",
        }
        return replacement, unresolved

    def test_all_four_candidate_codes_are_deferred(self) -> None:
        for code in (
            "candidate_scope",
            "owner_support",
            "duplicate_target",
            "target_overlap",
        ):
            with self.subTest(code=code):
                case = CandidateReductionTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                changes, unresolved = [case.candidate()], []
                expected = ("rejected_authority", "changes", 0)
                if code == "candidate_scope":
                    changes = [
                        case.note(scope={"project_id": "other", "task": None})
                    ]
                    expected = ("rejected_scope", "changes", 0)
                elif code in {"duplicate_target", "target_overlap"}:
                    replacement, proposal = case.target_batch()
                    changes = (
                        [replacement, replacement]
                        if code == "duplicate_target"
                        else [replacement]
                    )
                    unresolved = [proposal] if code == "target_overlap" else []
                    expected = (
                        ("rejected_revision", "changes", 1)
                        if code == "duplicate_target"
                        else ("rejected_revision", "unresolved", 0)
                    )
                before = case.counts()
                with self.assertRaisesRegex(
                    ProposalRejectedError, f"^{expected[0]}$"
                ) as raised:
                    case.deferred(case.result(changes, unresolved))
                feedback = raised.exception.feedback
                self.assertEqual(
                    (feedback.code, feedback.collection, feedback.index),
                    (code, *expected[1:]),
                )
                self.assertEqual(case.attempt(), (None, None))
                self.assertEqual(case.counts(), before)
                self.assertFalse(case.conn.in_transaction)

    def test_terminal_targets_win_in_either_order(self) -> None:
        for fault in (
            "missing",
            "stale",
            "scope",
            "repeated_affected",
            "overlap_stale",
            "unresolved_ref",
            "unresolved_target",
        ):
            for reverse in (False, True):
                with self.subTest(fault=fault, reverse=reverse):
                    case = CandidateReductionTests()
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    replacement, proposal = case.target_batch()
                    invalid = dict(replacement)
                    if fault == "missing":
                        invalid["knowledge_id"] = "K_" + "f" * 32
                    elif fault in {"stale", "overlap_stale"}:
                        invalid["expected_revision"] = 2
                    elif fault == "scope":
                        case.conn.execute(
                            "UPDATE knowledge_version SET scope_json=?",
                            ('{"project_id":"other","task":null}',),
                        )
                    eligible = case.note(
                        scope={"project_id": "other", "task": None}
                    )
                    changes = (
                        [invalid, eligible] if reverse else [eligible, invalid]
                    )
                    unresolved = []
                    if fault == "repeated_affected":
                        proposal["affected"] = [
                            {
                                "knowledge_id": replacement["knowledge_id"],
                                "revision": 1,
                            }
                        ] * 2
                        changes, unresolved = [eligible], [proposal]
                    elif fault == "overlap_stale":
                        proposal["affected"] = [
                            {
                                "knowledge_id": replacement["knowledge_id"],
                                "revision": 2,
                            }
                        ]
                        changes, unresolved = [eligible, replacement], [
                            proposal
                        ]
                    elif fault in {"unresolved_ref", "unresolved_target"}:
                        bad = {**proposal, "evidence_refs": ["E_" + "f" * 32]}
                        if fault == "unresolved_target":
                            bad = {
                                **proposal,
                                "affected": [
                                    {
                                        "knowledge_id": "K_" + "f" * 32,
                                        "revision": 1,
                                    }
                                ],
                            }
                        changes = [replacement]
                        unresolved = (
                            [bad, proposal] if reverse else [proposal, bad]
                        )
                    before = case.counts()
                    code = (
                        "rejected_scope"
                        if fault == "scope"
                        else (
                            "rejected_evidence"
                            if fault == "unresolved_ref"
                            else "rejected_revision"
                        )
                    )
                    with self.assertRaisesRegex(
                        ValueError, f"^{code}$"
                    ) as raised:
                        case.deferred(case.result(changes, unresolved))
                    self.assertFalse(hasattr(raised.exception, "feedback"))
                    self.assertEqual(case.attempt(), (13.0, code))
                    self.assertEqual(case.counts(), before)

    def test_first_feedback_survives_later_eligible_failures(self) -> None:
        with self.assertRaisesRegex(
            ProposalRejectedError, "^rejected_authority$"
        ) as raised:
            self.deferred(
                self.result(
                    [
                        self.candidate(),
                        self.note(scope={"project_id": "other", "task": None}),
                    ]
                )
            )
        self.assertEqual(raised.exception.feedback.code, "owner_support")
        self.assertEqual(raised.exception.feedback.index, 0)


class ExactReductionTests(CommitCase):
    """Accept only removals from each exact canonical ordered collection."""

    def proposal(self, claim: str = "Unknown") -> dict[str, object]:
        """Supply a typed unresolved candidate with no invented authority."""
        return {
            "claim": claim,
            "affected": [],
            "evidence_refs": [],
            "question": "Which evidence is missing?",
        }

    def reduce(self, original: ReviewResult, corrected: ReviewResult) -> None:
        """Call the exact-subset validator under test."""
        review_correction.require_reduction(original, corrected)

    def test_reduction_preserves_order_and_duplicate_multiplicity(
        self,
    ) -> None:
        first, second = self.note(), self.note(content="Second")
        one, two = self.proposal(), self.proposal("Other")
        original = self.result([first, first, second], [one, one, two])
        for changes, unresolved in (
            ([first, second], [one, two]),
            ([first, first], [one, one, two]),
            ([], [one]),
        ):
            self.reduce(original, self.result(changes, unresolved))
        for changes, unresolved in (
            ([first, first, first], [one]),
            ([second, first], []),
            ([], [two, one]),
            ([], [one, one, one]),
        ):
            with self.assertRaises(ValueError):
                self.reduce(original, self.result(changes, unresolved))

    def test_reduction_rejects_every_semantic_edit(self) -> None:
        original = self.result(
            [self.note(), self.note(content="Drop")], [self.proposal()]
        )
        edits: dict[str, object] = {
            "content": "Changed",
            "reason": "Changed",
            "conditions": ["Changed"],
            "kind": "preference",
            "evidence_refs": [self.evidence[1].id],
            "scope": {"project_id": "other", "task": None},
        }
        for field, value in edits.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.reduce(
                    original, self.result([self.note(**{field: value})])
                )
        for field, value in {
            "claim": "Changed",
            "question": "Changed",
            "evidence_refs": [self.evidence[0].id],
            "affected": [{"knowledge_id": "K_" + "a" * 32, "revision": 1}],
        }.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.reduce(
                    original,
                    self.result([], [{**self.proposal(), field: value}]),
                )
        with self.assertRaises(ValueError):
            self.reduce(original, original)
        with self.assertRaises(ValueError):
            self.reduce(original, self.result([], [self.proposal("Invented")]))

    def test_reduction_rejects_target_action_and_dependency_edits(
        self,
    ) -> None:
        replacement = self.note(
            action="replace", knowledge_id="K_" + "a" * 32, expected_revision=1
        )
        original = self.result([replacement, self.note(content="Drop")])
        for edit in (
            {"knowledge_id": "K_" + "b" * 32},
            {"expected_revision": 2},
        ):
            with self.assertRaises(ValueError):
                self.reduce(original, self.result([{**replacement, **edit}]))
        with self.assertRaises(ValueError):
            self.reduce(original, self.result([self.note()]))
        claim = {"text": "Use Python", "evidence_refs": [self.evidence[0].id]}
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Guide",
            "summary": claim,
            "applicability": [],
            "decision_points": [],
            "pitfalls": [],
            "reason": "Supported",
            "steps": [
                {"key": "one", "claim": claim, "depends_on": []},
                {"key": "two", "claim": claim, "depends_on": ["one"]},
            ],
        }
        original = self.result([guide, self.note()])
        changed = deepcopy(guide)
        changed["steps"][1]["depends_on"] = []
        with self.assertRaises(ValueError):
            self.reduce(original, self.result([changed]))

    def test_empty_reduction_requires_fixed_reason(self) -> None:
        original = self.result([self.note()])
        empty = self.result([])
        with self.assertRaises(ValueError):
            self.reduce(original, empty)
        corrected = empty.model_copy(
            update={
                "no_change_reason": (
                    "No supported candidates remain " "after correction."
                )
            }
        )
        self.reduce(original, corrected)
        with self.assertRaises(ValueError):
            self.reduce(corrected, corrected)
        invalid = original.model_copy(update={"no_change_reason": "Invented"})
        with self.assertRaisesRegex(ValueError, "^rejected_validation$"):
            self.reduce(self.result([self.note(), self.note()]), invalid)
