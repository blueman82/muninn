"""Exercise targeted and supervisor continuation without execution stubs."""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

from muninn import decision_requests, review_requests, store
from muninn.decision_write import submit_intent
from muninn.review_supervisor import Supervisor
from tests.decision_support import DecisionCase


class DecisionContinuationTests(DecisionCase):
    """Keep deterministic work separate from model execution and ownership."""

    def submit(self, ident: str) -> None:
        """Submit a physically backed locator-only approval."""
        intent = self.intent(ident)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))

    def test_targeted_apply_never_selects_another_candidate(self) -> None:
        a, b = self.stage(), self.stage()
        self.submit(a.id)
        self.submit(b.id)
        before = self.row("SELECT count(*) FROM review_attempt")[0]
        with (
            mock.patch.object(Supervisor, "start", side_effect=AssertionError),
            mock.patch(
                "muninn.review_supervisor.create_wrapper",
                side_effect=AssertionError,
            ),
        ):
            result = decision_requests.request_application(
                self.home, b.id, 25.0
            )
        self.assertEqual(
            (result.id, result.state, result.receipt),
            (b.id, "approved_pending", None),
        )
        self.assertEqual(
            self.row(
                "SELECT revision,application_request_id FROM "
                "memory_candidate WHERE id=?",
                (a.id,),
            )[0],
            0,
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_attempt")[0], before
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_continuation_is_bounded_and_generation_fenced(self) -> None:
        a, b = self.stage(), self.stage()
        self.submit(a.id)
        self.submit(b.id)
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            decision_requests.continue_decisions(self.home, 2, 25.0)
        views = decision_requests.continue_decisions(self.home, 1, 25.0)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].state, "approved_pending")
        self.assertEqual(
            self.row("SELECT count(*) FROM memory_candidate WHERE revision=1")[
                0
            ],
            1,
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id IS "
                "NOT NULL"
            )[0],
            1,
        )

    def test_model_claim_excludes_due_and_recovered_applications(self) -> None:
        candidate = self.stage()
        self.approve(self.intent(candidate.id))
        application = self.transaction(
            lambda: decision_requests.admit_application(
                self.conn, self.home, candidate.id, 1, 25.0
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 26.0
            )
        )
        supervisor = Supervisor(self.home, self.policy)
        supervisor.generation = 1
        self.assertIsNone(supervisor._claim(27.0))
        self.assertEqual(
            self.row(
                "SELECT state FROM review_request WHERE id=?",
                (application.id,),
            )[0],
            "pending",
        )

    def test_waiting_index_keeps_locator_only_without_evidence(self) -> None:
        candidate = self.stage()
        intent = self.fork_intent(candidate.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))
        evidence = self.row("SELECT count(*) FROM review_evidence")[0]
        view = decision_requests.request_application(
            self.home, candidate.id, 25.0
        )
        self.assertEqual(
            (view.state, view.code), ("waiting_verification", "waiting_index")
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], evidence
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id IS "
                "NOT NULL"
            )[0],
            0,
        )

    def test_accept_failure_rolls_back_new_evidence_before_status(
        self,
    ) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        before = self.row("SELECT count(*) FROM review_evidence")[0]
        with mock.patch(
            "muninn.decision_requests.accept_decision",
            side_effect=ValueError("source_unavailable"),
        ):
            view = decision_requests.request_application(
                self.home, candidate.id, 25.0
            )
        self.assertEqual(view.code, "rejected_evidence")
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], before
        )
        row = self.row(
            "SELECT accepted_revision,owner_evidence_id,display_evidence_id "
            "FROM memory_decision WHERE candidate_id=?",
            (candidate.id,),
        )
        self.assertEqual(tuple(row), (None, None, None))

    def test_unknown_refusal_is_finite_and_rolls_back_proof(self) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        before = self.row("SELECT count(*) FROM review_evidence")[0]
        with mock.patch(
            "muninn.decision_requests.accept_decision",
            side_effect=ValueError("private provider details"),
        ):
            view = decision_requests.request_application(
                self.home, candidate.id, 25.0
            )
        self.assertEqual(view.code, "decision_invalid")
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], before
        )

    def test_replay_refuses_missing_application_association(self) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        decision_requests.request_application(self.home, candidate.id, 25.0)
        application = self.row(
            "SELECT application_request_id FROM memory_candidate WHERE id=?",
            (candidate.id,),
        )[0]
        self.conn.execute(
            "UPDATE memory_candidate SET application_request_id=NULL WHERE "
            "id=?",
            (candidate.id,),
        )
        view = decision_requests.request_application(
            self.home, candidate.id, 26.0
        )
        self.assertEqual(view.code, "rejected_fenced")
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            1,
        )
        self.assertEqual(
            self.row(
                "SELECT id FROM review_request WHERE candidate_id=?",
                (candidate.id,),
            )[0],
            application,
        )

    def test_root_contention_refuses_and_next_operation_releases(self) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        code = """
import sys
from pathlib import Path
from muninn import decision_requests, store
home, ident = Path(sys.argv[1]), sys.argv[2]
with store.writer_lock(home, wait_s=0):
    try:
        decision_requests.request_application(home, ident, 25.0)
    except store.BusyError:
        pass
    else:
        raise AssertionError('nested root accepted')
view = decision_requests.request_application(home, ident, 26.0)
assert view.state == 'approved_pending'
with store.writer_lock(home, wait_s=0):
    pass
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.home), candidate.id],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_paused_admission_still_checks_complete_display(self) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        small = self.policy.model_copy(update={"context_bytes": 32})
        self.conn.execute(
            "UPDATE review_control SET "
            "admission=0,policy_json=?,policy_digest=?",
            (small.model_dump_json(), self.digest(small)),
        )
        before = self.row("SELECT count(*) FROM review_evidence")[0]
        view = decision_requests.request_application(
            self.home, candidate.id, 25.0
        )
        self.assertEqual(
            (view.state, view.code),
            ("waiting_verification", "decision_display_limit"),
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], before
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM memory_decision WHERE "
                "accepted_revision IS NOT NULL"
            )[0],
            0,
        )

    def test_saved_continuation_preserves_receipt_and_unaccepted_intent(
        self,
    ) -> None:
        candidate, replacement = self.stage(), self.stage()
        self.approve(self.intent(candidate.id))
        late = self.intent(
            candidate.id, action="edit", replacement_id=replacement.id
        )
        self.transaction(lambda: submit_intent(self.conn, late, 24.0))
        receipt = self.saved_fixture(candidate.id)
        views = decision_requests.continue_decisions(self.home, 1, 30.0)
        self.assertEqual(len(views), 1)
        self.assertEqual(
            (views[0].state, views[0].receipt), ("saved", receipt)
        )
        row = self.row(
            "SELECT "
            "state,code,accepted_revision,owner_evidence_id,"
            "display_evidence_id FROM memory_decision "
            "WHERE candidate_id=? AND action='edit'",
            (candidate.id,),
        )
        self.assertEqual(
            tuple(row), ("blocked", "already_saved", None, None, None)
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT state,revision,application_request_id FROM "
                    "memory_candidate WHERE id=?",
                    (replacement.id,),
                )
            ),
            ("waiting_verification", 0, None),
        )
        self.assertEqual(
            decision_requests.continue_decisions(self.home, 1, 31.0), ()
        )

    def test_stale_owner_policy_or_generation_stops_continuation_setup(
        self,
    ) -> None:
        supervisor = Supervisor(self.home, self.policy)
        supervisor.generation = 1
        self.conn.execute("UPDATE review_control SET generation=2")
        with mock.patch.object(
            supervisor,
            "_inputs",
            side_effect=AssertionError("stale owner setup"),
        ):
            self.assertFalse(supervisor._ready_to_spawn(25.0))
        self.conn.execute(
            "UPDATE review_control SET generation=1,policy_digest=?",
            ("f" * 64,),
        )
        with mock.patch.object(
            supervisor,
            "_inputs",
            side_effect=AssertionError("stale policy setup"),
        ):
            self.assertFalse(supervisor._ready_to_spawn(25.0))

    def test_tick_continues_one_decision_before_ordinary_claim(self) -> None:
        candidate = self.stage()
        self.submit(candidate.id)
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 24.0
            )
        )
        supervisor = Supervisor(self.home, self.policy)
        supervisor.generation = 1
        supervisor._owner = 1
        supervisor.monitor = mock.Mock()
        supervisor.monitor.drain.return_value = []
        supervisor._catchup = (0, 100.0)
        with (
            mock.patch.object(supervisor, "_inputs", return_value=True),
            mock.patch(
                "muninn.review_supervisor.create_wrapper",
                side_effect=AssertionError,
            ),
        ):
            supervisor.tick(25.0)
        view = decision_requests.decision_view(self.conn, candidate.id)
        self.assertEqual(view.state, "saved")
        self.assertIsNotNone(view.receipt)
        self.assertIsNotNone(
            self.row(
                "SELECT application_request_id FROM memory_candidate WHERE "
                "id=?",
                (candidate.id,),
            )[0]
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE state='running'"
            )[0],
            0,
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
