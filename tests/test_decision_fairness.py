"""Continue decisions fairly across bounded passes and owner restarts."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from functools import partial
from unittest import mock
from uuid import UUID

from muninn import decision_requests, review_requests
from muninn.decision_write import submit_intent
from muninn.review_supervisor import Supervisor
from tests.decision_support import DecisionCase
from tests.review_pause_support import initialize


class DecisionFairnessTests(DecisionCase):
    """Exercise advisory queue order without changing decision authority."""

    def waiting_pair(self) -> tuple[str, str]:
        """Stage a known earlier waiting fork and later ready primary proof."""
        with mock.patch(
            "muninn.decision_candidates.uuid4",
            side_effect=[UUID(int=1), UUID(int=2)],
        ):
            a, b = self.stage(), self.stage()
        ready = self.intent(b.id)
        waiting = self.fork_intent(a.id)
        self.transaction(lambda: submit_intent(self.conn, ready, 20.0))
        self.transaction(lambda: submit_intent(self.conn, waiting, 20.0))
        return a.id, b.id

    def test_waiting_head_yields_with_maximum_single_slot_batch(self) -> None:
        a, b = self.waiting_pair()
        self.assertEqual(self.policy.concurrency, 1)
        first = decision_requests.continue_decisions(
            self.home, 1, 25.0, limit=self.policy.concurrency
        )
        self.assertEqual((first[0].id, first[0].code), (a, "waiting_index"))
        second = decision_requests.continue_decisions(
            self.home, 1, 26.0, limit=self.policy.concurrency
        )
        self.assertEqual(
            (second[0].id, second[0].state), (b, "approved_pending")
        )
        self.assertIsNotNone(
            self.row(
                "SELECT application_request_id FROM memory_candidate "
                "WHERE id=?",
                (b,),
            )[0]
        )
        self.assertEqual(
            self.row("SELECT revision FROM memory_candidate WHERE id=?", (a,))[
                0
            ],
            0,
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)

    def test_actual_tick_yields_waiting_head_to_ready_candidate(self) -> None:
        a, b = self.waiting_pair()
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
            for now in (25.0, 26.0, 27.0):
                supervisor.tick(now)
        self.assertEqual(
            self.row(
                "SELECT state,revision FROM memory_candidate WHERE id=?", (b,)
            )["revision"],
            1,
        )
        self.assertIsNotNone(
            self.row(
                "SELECT application_request_id FROM memory_candidate "
                "WHERE id=?",
                (b,),
            )[0]
        )
        self.assertEqual(
            self.row("SELECT revision FROM memory_candidate WHERE id=?", (a,))[
                0
            ],
            0,
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE state='running'"
            )[0],
            0,
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)

    def test_wrap_reconsiders_late_ready_without_changing_canonical_data(
        self,
    ) -> None:
        a, b = self.waiting_pair()
        canonical = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT id,payload_json,digest,created_at,"
                "normalization_version,"
                "enrichment_version FROM memory_candidate ORDER BY id"
            )
        ]
        displays = self.display(a), self.display(b)
        attempts = self.row("SELECT count(*) FROM review_attempt")[0]
        visited = [
            decision_requests.continue_decisions(self.home, 1, now)[0].id
            for now in (25.0, 26.0, 27.0)
        ]
        self.assertEqual(visited, [a, b, a])
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT id,payload_json,digest,created_at,"
                    "normalization_version,"
                    "enrichment_version FROM memory_candidate ORDER BY id"
                )
            ],
            canonical,
        )
        self.assertEqual((self.display(a), self.display(b)), displays)
        earlier = tuple(
            self.row(
                "SELECT * FROM memory_decision WHERE candidate_id=?", (b,)
            )
        )
        late = self.intent(a)
        self.transaction(lambda: submit_intent(self.conn, late, 30.0))
        views = decision_requests.continue_decisions(self.home, 1, 31.0)
        self.assertEqual(
            (views[0].id, views[0].state), (a, "approved_pending")
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM memory_decision WHERE candidate_id=?", (b,)
                )
            ),
            earlier,
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_attempt")[0], attempts
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)

    def test_blocked_approval_yields_and_already_queued_work_is_skipped(
        self,
    ) -> None:
        receipt = self.save(self.result([self.note()]))
        ref = receipt.applied[0]
        with mock.patch(
            "muninn.decision_candidates.uuid4",
            side_effect=[UUID(int=1), UUID(int=2)],
        ):
            a = self.stage(
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
            b = self.stage()
        self.approve(self.intent(a.id))
        intent = self.intent(b.id)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))
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
        earlier = tuple(
            self.row(
                "SELECT * FROM memory_decision WHERE candidate_id=?", (a.id,)
            )
        )
        first = decision_requests.continue_decisions(self.home, 1, 25.0)
        self.assertEqual(
            (first[0].id, first[0].code), (a.id, "rejected_revision")
        )
        second = decision_requests.continue_decisions(self.home, 1, 26.0)
        self.assertEqual(
            (second[0].id, second[0].state), (b.id, "approved_pending")
        )
        third = decision_requests.continue_decisions(self.home, 1, 27.0)
        self.assertEqual(
            (third[0].id, third[0].code), (a.id, "rejected_revision")
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM memory_decision WHERE candidate_id=?",
                    (a.id,),
                )
            ),
            earlier,
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (b.id,),
            )[0],
            1,
        )

    def test_materialized_batch_processes_unique_candidates_with_one_budget(
        self,
    ) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        self.policy = self.policy.model_copy(update={"concurrency": 2})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        self.context = self.start()
        for candidate in (self.stage(), self.stage()):
            intent = self.intent(candidate.id)
            self.transaction(partial(submit_intent, self.conn, intent, 20.0))
        before = self.row("SELECT max(rowid) FROM memory_candidate")[0]
        with mock.patch(
            "muninn.decision_requests.verify_decision",
            wraps=decision_requests.verify_decision,
        ) as verify:
            views = decision_requests.continue_decisions(
                self.home, 1, 25.0, limit=2
            )
        self.assertEqual(len({view.id for view in views}), 2)
        self.assertTrue(
            all(view.state == "approved_pending" for view in views)
        )
        self.assertEqual(verify.call_count, 2)
        self.assertIs(
            verify.call_args_list[0].args[-1],
            verify.call_args_list[1].args[-1],
        )
        self.assertTrue(
            all(
                row[0] > before
                for row in self.conn.execute(
                    "SELECT rowid FROM memory_candidate"
                )
            )
        )

    def test_queue_update_failure_rolls_back_order_and_proof_unit(
        self,
    ) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        self.policy = self.policy.model_copy(update={"concurrency": 2})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        self.context = self.start()
        candidates = self.stage(), self.stage()
        for offset, candidate in enumerate(candidates):
            intent = self.intent(candidate.id)
            self.transaction(partial(submit_intent, self.conn, intent, 20.0))
            self.conn.execute(
                "UPDATE memory_candidate SET rowid=? WHERE id=?",
                (9223372036854775805 + offset, candidate.id),
            )
        before = self.projection()
        order = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT rowid,id FROM memory_candidate ORDER BY rowid"
            )
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            decision_requests.continue_decisions(self.home, 1, 25.0, limit=2)
        self.assertEqual(self.projection(), before)
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT rowid,id FROM memory_candidate ORDER BY rowid"
                )
            ],
            order,
        )

    def test_one_tick_in_each_fresh_process_retains_fair_order(self) -> None:
        a, b = self.waiting_pair()
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 24.0
            )
        )
        initialize(self.home)
        code = """
import sys
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from functools import partial
from unittest import mock
from muninn import store
from muninn.review_models import ReviewPolicy
from muninn.review_supervisor import Supervisor
home, now = Path(sys.argv[1]), float(sys.argv[2])
with closing(store.connect_ro(store.db_path(home))) as conn:
    policy = ReviewPolicy.model_validate_json(conn.execute(
        'SELECT policy_json FROM review_control').fetchone()[0])
with mock.patch.object(Supervisor, '_inputs', return_value=True), mock.patch(
        'muninn.review_supervisor.create_wrapper', side_effect=AssertionError):
    supervisor = Supervisor(home, policy)
    supervisor.start(now)
    try:
        supervisor._catchup = (0, 100.0)
        supervisor.tick(now)
    finally:
        supervisor.stop(now + 0.5)
"""
        for now in (25.0, 26.0):
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.home), str(now)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.row("SELECT revision FROM memory_candidate WHERE id=?", (a,))[
                0
            ],
            0,
        )
        self.assertEqual(
            self.row("SELECT revision FROM memory_candidate WHERE id=?", (b,))[
                0
            ],
            1,
        )
        self.assertIsNotNone(
            self.row(
                "SELECT application_request_id FROM memory_candidate "
                "WHERE id=?",
                (b,),
            )[0]
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)
