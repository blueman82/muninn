"""Fence new local proposals behind unapplied durable erasure intent."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from muninn import (
    review_budget,
    review_originals,
    review_pause,
    review_requests,
    store,
    tombstones,
)
from muninn.review_models import AttemptView, ReviewContext
from muninn.tombstone_key import TOMBSTONE_FILE
from tests.ingest_support import TID
from tests.review_pause_support import initialize, interrupt_erasure
from tests.test_review_commit import CommitCase


class ProposalPendingTests(CommitCase):
    """Exercise real journal authority with disposable requests and stores."""

    def claim(self, generation: int = 1) -> AttemptView:
        """Use the public local claim inside an already owned transaction."""
        return review_requests.claim_proposal(
            self.conn, self.context.request_id, generation, 15.0
        )

    def state(self) -> tuple[str, ...]:
        """Snapshot durable tables for exact mutation and rollback checks."""
        return tuple(self.conn.iterdump())

    def append_intent(self) -> None:
        """Durably append an affected thread without applying its erasure."""
        tombstones.append_tombstones(
            self.home,
            [tombstones.tombstone_row("codex", "thread", thread_id=TID)],
        )

    def reserve_history(self) -> None:
        """Retain an unknown charge and a launched call on the old attempt."""
        for index in (1, 2):
            context = ReviewContext(
                request_id=self.context.request_id,
                attempt_id=self.context.attempt_id,
                generation=1,
                scope=self.scope,
                policy=self.policy,
                admitted_windows=(self.window(),),
                evidence_ids=self.context.evidence_ids,
                round_index=index,
                deadline=72.0,
            )
            self.transaction(
                lambda value=context: review_budget.reserve_call(
                    self.conn, value, 13.0
                )
            )
            self.conn.execute(
                "UPDATE review_call SET state='completed' WHERE round_index=?",
                (index,),
            )
        self.conn.execute(
            "UPDATE review_call SET state='unknown',ended_at=14.0,"
            "outcome='recovered' WHERE round_index=1"
        )
        self.conn.execute(
            "UPDATE review_call SET state='launched',launched_at=14.5 "
            "WHERE round_index=2"
        )

    def test_pending_intent_refuses_before_reads_or_any_mutation(self) -> None:
        self.reserve_history()
        before = self.state()
        self.append_intent()
        self.assertEqual(self.state(), before)
        journal = (self.home / TOMBSTONE_FILE).read_bytes()
        with store.writer_lock(self.home, wait_s=0):
            self.conn.execute("BEGIN IMMEDIATE")
            changes = self.conn.total_changes
            try:
                with (
                    mock.patch.object(
                        review_originals,
                        "_jsonl",
                        side_effect=AssertionError("original read"),
                    ),
                    self.assertRaisesRegex(
                        ValueError, "^erasure_pending$"
                    ) as caught,
                ):
                    self.claim()
                self.assertIs(type(caught.exception), ValueError)
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.conn.total_changes, changes)
                self.assertEqual(self.state(), before)
            finally:
                self.conn.execute("ROLLBACK")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.state(), before)
        self.assertEqual((self.home / TOMBSTONE_FILE).read_bytes(), journal)

    def test_pending_precedes_invalid_current_policy_and_source(self) -> None:
        self.append_intent()
        before = self.state()
        for sql in (
            "UPDATE review_control SET policy_json='{}'",
            "UPDATE review_control SET admission=0",
            "UPDATE source SET status='missing'",
            "UPDATE review_control SET generation=2",
        ):
            with self.subTest(sql=sql), store.writer_lock(self.home, wait_s=0):
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(sql)
                    changed = self.state()
                    changes = self.conn.total_changes
                    with self.assertRaisesRegex(
                        ValueError, "^erasure_pending$"
                    ) as caught:
                        self.claim()
                    self.assertIs(type(caught.exception), ValueError)
                    self.assertTrue(self.conn.in_transaction)
                    self.assertEqual(self.conn.total_changes, changes)
                    self.assertEqual(self.state(), changed)
                finally:
                    self.conn.execute("ROLLBACK")
            self.assertEqual(self.state(), before)

    def test_receipt_precedes_pending_journal_and_all_mutable_checks(
        self,
    ) -> None:
        receipt = self.save(self.result([self.note()]))
        self.append_intent()
        self.conn.execute(
            "UPDATE review_control "
            "SET admission=0,generation=2,policy_json='{}'"
        )
        self.path.unlink()
        before = self.state()
        with store.writer_lock(self.home, wait_s=0):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                with (
                    mock.patch.object(
                        Path, "stat", side_effect=AssertionError("stat")
                    ),
                    mock.patch.object(
                        tombstones,
                        "read_intent",
                        side_effect=AssertionError("journal read"),
                    ),
                    mock.patch.object(
                        review_originals,
                        "_jsonl",
                        side_effect=AssertionError("original read"),
                    ),
                ):
                    for generation in (0, 1, 2):
                        self.assertEqual(
                            self.claim(generation).id, receipt.attempt_id
                        )
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.state(), before)
            finally:
                self.conn.execute("ROLLBACK")
        self.assertEqual(self.state(), before)

    def test_paused_replay_keeps_affected_request_fenced(self) -> None:
        initialize(self.home)
        interrupt_erasure(self.home, TID)
        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaisesRegex(ValueError, "^erasure_pending$"),
        ):
            self.transaction(self.claim)
        with review_pause.erase_connection(
            self.home, wait_s=0, status=review_pause.EraseStatus()
        ):
            self.assertEqual(self.row("SELECT count(*) FROM tombstone")[0], 1)
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT state,current_attempt_id,cancel_code "
                    "FROM review_request"
                )
            ),
            ("cancelled", None, "evidence_erased"),
        )
        self.assertEqual(
            self.row("SELECT outcome FROM review_attempt")[0],
            "evidence_erased",
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_evidence "
                "WHERE state!='erased' OR excerpt IS NOT NULL"
            )[0],
            0,
        )
        before = self.state()
        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaisesRegex(ValueError, "^source_unavailable$"),
        ):
            self.transaction(self.claim)
        self.assertEqual(self.state(), before)

    def test_absent_journal_preserves_caller_owned_claim_and_rollback(
        self,
    ) -> None:
        self.assertFalse((self.home / TOMBSTONE_FILE).exists())
        before = self.state()
        with store.writer_lock(self.home, wait_s=0):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                local = self.claim()
                self.assertTrue(self.conn.in_transaction)
                self.assertNotEqual(local.id, self.context.attempt_id)
                self.assertIsNone(local.requested)
                self.assertEqual(local.outcome, "operator_retry")
                self.assertEqual(
                    self.row(
                        "SELECT ended_at,outcome FROM review_attempt "
                        "WHERE id=?",
                        (self.context.attempt_id,),
                    )[:],
                    (15.0, "operator_retry"),
                )
            finally:
                self.conn.execute("ROLLBACK")
        self.assertEqual(self.state(), before)

    def test_pending_intent_does_not_change_automatic_claim_behavior(
        self,
    ) -> None:
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, self.context.attempt_id, "config", 13.0
            )
        )
        self.transaction(
            lambda: review_requests.retry_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        self.append_intent()
        with store.writer_lock(self.home, wait_s=0):
            attempt = self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, self.context.request_id, 1, 15.0
                )
            )
        self.assertNotEqual(attempt.id, self.context.attempt_id)
        self.assertIsNotNone(attempt.requested)
        self.assertEqual(
            self.row("SELECT origin FROM review_request")[0], "review"
        )
        self.assertEqual(self.row("SELECT count(*) FROM tombstone")[0], 0)
