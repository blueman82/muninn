"""Exercise distinct, checked application admission against real originals."""

from __future__ import annotations

from unittest import mock

from muninn import decision_requests, review_requests
from muninn.decision_write import submit_intent
from muninn.review_history import _covered, replacements
from muninn.review_progress import advance_completed, unsuppressed_window
from muninn.review_state import LearningSelection
from tests.decision_support import DecisionCase


class DecisionRequestTests(DecisionCase):
    """Preserve ordinary identity and progress while admitting decisions."""

    def test_independent_application_identity_vector(self) -> None:
        ident = "D_" + "1" * 32
        frozen = "2" * 64
        expected = (
            "9da564911d924937ab7d30f40c12ee772fdf7bf0a6b731476e7cc6f7ca585dd7"
        )
        key = decision_requests._application_key(ident, frozen, 7)
        self.assertEqual(key, expected)
        for args in (
            ("D_" + "3" * 32, frozen, 7),
            (ident, "4" * 64, 7),
            (ident, frozen, 8),
        ):
            self.assertNotEqual(decision_requests._application_key(*args), key)

    def application(self, ident: str, revision: int = 1) -> str:
        """Admit under the real caller-owned transaction."""
        return self.transaction(
            lambda: decision_requests.admit_application(
                self.conn, self.home, ident, revision, 25.0
            )
        ).id

    def test_application_identity_preserves_ordinary_dedup(self) -> None:
        a, b = self.stage(), self.stage()
        intent = self.intent(a.id)
        self.approve(intent)
        self.approve(self.intent(b.id))
        receipt = self.save(self.result([self.note()]))
        before = self.conn.execute(
            "SELECT * FROM review_receipt WHERE request_id=?",
            (receipt.request_id,),
        ).fetchone()
        ordinary = self.enqueue(self.window())
        key = self.row(
            "SELECT duplicate_key FROM review_request WHERE id=?",
            (ordinary.id,),
        )[0]
        first, second = self.application(a.id), self.application(b.id)
        self.assertEqual(len({ordinary.id, first, second}), 3)
        self.assertEqual(self.application(a.id), first)
        self.assertEqual(self.enqueue(self.window()).id, ordinary.id)
        self.assertEqual(
            self.row(
                "SELECT duplicate_key FROM review_request WHERE id=?",
                (ordinary.id,),
            )[0],
            key,
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM review_receipt WHERE request_id=?",
                    (receipt.request_id,),
                )
            ),
            tuple(before),
        )
        app = self.row("SELECT * FROM review_request WHERE id=?", (first,))
        self.assertEqual(
            (app["from_line"], app["through_line"]),
            (intent.owner_locator.line, intent.owner_locator.line),
        )
        self.assertEqual(
            (app["origin"], app["trigger"], app["state"]),
            ("proposal", "explicit", "pending"),
        )
        inputs = {
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=?",
                (first,),
            )
        }
        self.assertEqual(
            inputs,
            {
                row[0]
                for row in self.conn.execute(
                    "SELECT evidence_id FROM memory_candidate_support WHERE "
                    "candidate_id=?",
                    (a.id,),
                )
            },
        )

    def test_unaccepted_negative_preserves_active_approval(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        negative = self.intent(candidate.id, action="reject", wording="No.")
        self.transaction(lambda: submit_intent(self.conn, negative, 24.0))
        result = decision_requests.request_application(
            self.home, candidate.id, 25.0
        )
        self.assertEqual(
            (result.state, result.revision), ("approved_pending", 1)
        )
        self.assertIsNotNone(
            self.row(
                "SELECT application_request_id FROM memory_candidate WHERE "
                "id=?",
                (candidate.id,),
            )[0]
        )

    def test_current_policy_change_never_rebinds_existing_pair(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        application = self.application(candidate.id)
        before = tuple(
            self.row("SELECT * FROM review_request WHERE id=?", (application,))
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        view = decision_requests.request_application(
            self.home, candidate.id, 26.0
        )
        self.assertEqual(view.code, "learning_paused")
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM review_request WHERE id=?", (application,)
                )
            ),
            before,
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            1,
        )

    def test_application_is_not_source_coverage_or_cancellation(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        application = self.application(candidate.id)
        window = self.row(
            "SELECT from_line,through_line FROM review_request WHERE id=?",
            (application,),
        )
        owner_window = self.window(window[0], window[1])
        source = self.need_source()
        progress = tuple(self.row("SELECT * FROM review_source"))
        self.assertNotIn(
            tuple(window), _covered(self.conn, source, self.policy)
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, application, 26.0
            )
        )
        self.assertEqual(
            unsuppressed_window(self.conn, owner_window), owner_window
        )
        self.assertNotIn(
            tuple(window), _covered(self.conn, source, self.policy)
        )
        self.transaction(
            lambda: advance_completed(self.conn, owner_window.source, 27.0)
        )
        after = self.row("SELECT * FROM review_source")
        self.assertEqual(tuple(after)[:-1], progress[:-1])

    def test_approval_revisions_have_separate_requests(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        first = self.application(candidate.id)
        self.approve(self.intent(candidate.id))
        second = self.application(candidate.id, 2)
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.application(candidate.id, 1)

    def test_saved_foreground_returns_original_receipt(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        receipt = self.saved_fixture(candidate.id)
        before = self.projection()
        with mock.patch(
            "muninn.review_supervisor.Supervisor.start",
            side_effect=AssertionError,
        ):
            view = decision_requests.request_application(
                self.home, candidate.id, 30.0
            )
        self.assertEqual((view.state, view.receipt), ("saved", receipt))
        self.assertEqual(self.projection(), before)

    def test_application_receipt_never_advances_ordinary_completion(
        self,
    ) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        self.saved_fixture(candidate.id)
        self.conn.execute("UPDATE review_source SET completed_line=1")
        completed = self.transaction(
            lambda: advance_completed(self.conn, self.window().source, 30.0)
        )
        self.assertEqual(completed, 1)

    def test_changed_policy_preserves_historical_application(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        application = self.application(candidate.id)
        before = tuple(
            self.row("SELECT * FROM review_request WHERE id=?", (application,))
        )
        changed = self.policy.model_copy(update={"context_bytes": 1200})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (changed.model_dump_json(), self.digest(changed)),
        )
        result = decision_requests.request_application(
            self.home, candidate.id, 30.0
        )
        self.assertEqual(
            (result.state, result.code),
            ("approved_pending", "rejected_validation"),
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM review_request WHERE id=?", (application,)
                )
            ),
            before,
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            1,
        )

    def test_verified_reject_fences_older_application(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        application = self.application(candidate.id)
        self.approve(self.intent(candidate.id, action="reject"))
        self.assertEqual(
            decision_requests.request_application(
                self.home, candidate.id, 30.0
            ).state,
            "rejected",
        )
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.application(candidate.id, 1)
        self.assertEqual(
            self.row(
                "SELECT id FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            application,
        )

    def test_policy_replacement_does_not_turn_application_into_ordinary_work(
        self,
    ) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        self.application(candidate.id)
        self.conn.execute(
            "UPDATE review_request SET "
            "state='cancelled',cancel_code='policy_replaced'"
        )
        changed = self.policy.model_copy(update={"context_bytes": 1200})
        scan = replacements(
            self.conn,
            changed,
            LearningSelection(
                mode="all",
                project_id=self.scope.project_id,
                selected_at=30.0,
                scan_digest=None,
            ),
        )
        self.assertEqual(
            [
                (item.window.from_line, item.window.through_line)
                for item in scan.items
            ],
            [(2, 3)],
        )

    def test_changed_current_target_blocks_application_admission(self) -> None:
        receipt = self.save(self.result([self.note()]))
        ref = receipt.applied[0]
        candidate = self.stage(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=ref.knowledge_id,
                        expected_revision=ref.revision,
                    )
                ]
            )
        )
        self.approve(self.intent(candidate.id))
        self.context = self.start(2, 2)
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=ref.knowledge_id,
                        expected_revision=ref.revision,
                    )
                ]
            )
        )
        view = decision_requests.request_application(
            self.home, candidate.id, 30.0
        )
        self.assertEqual(
            (view.state, view.code), ("approved_pending", "rejected_revision")
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            0,
        )
