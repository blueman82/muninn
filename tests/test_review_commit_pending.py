"""Fence late foreground commits on unapplied durable erasure intent."""

from __future__ import annotations

from unittest import mock

from muninn import review_originals, review_quiescence, store, tombstones
from muninn.review_json import canonical_json
from muninn.review_requests import UNKNOWN_CONFIG
from muninn.tombstone_key import TOMBSTONE_FILE
from tests.review_quiescence_support import initialize, interrupt_erasure
from tests.test_review_commit import CommitCase


class PendingCommitTests(CommitCase):
    """Keep pending refusals read-only and completed receipts available."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)
        self.conn.execute(
            "INSERT INTO review_call(attempt_id,round_index,budget_account_id,"
            "policy_digest,state,reserved_at,launched_at,ended_at,"
            "requested_json,observed_json,cost_reservation_decimal,"
            "currency,outcome) VALUES(?,1,'test-account',?,'unknown',"
            "12.0,12.1,12.2,?,?,'0.75','EUR','response_lost')",
            (
                self.context.attempt_id,
                self.digest(self.policy),
                canonical_json(self.policy.runner),
                canonical_json(UNKNOWN_CONFIG),
            ),
        )

    def pending(self) -> bytes:
        """Interrupt a real erase entry after its durable journal append."""
        interrupt_erasure(self.home, self.evidence[0].thread_id)
        self.assertEqual(self.row("SELECT count(*) FROM tombstone")[0], 0)
        return (self.home / TOMBSTONE_FILE).read_bytes()

    def unchanged(self, before: list[str], changes: int) -> None:
        """Include content, normalized supports, FTS, history and charges."""
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT state,cost_reservation_decimal,currency,outcome "
                    "FROM review_call"
                )
            ),
            ("unknown", "0.75", "EUR", "response_lost"),
        )

    def test_pending_after_claim_rolls_back_without_reads_or_writes(
        self,
    ) -> None:
        journal = self.pending()
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        trace: list[str] = []
        self.conn.set_trace_callback(trace.append)
        try:
            with (
                store.writer_lock(self.home),
                mock.patch.object(
                    review_originals,
                    "open_regular",
                    wraps=review_originals.open_regular,
                ) as originals,
                self.assertRaises(ValueError) as caught,
            ):
                self.save(self.result([self.note()]))
        finally:
            self.conn.set_trace_callback(None)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, ("erasure_pending",))
        self.assertEqual(originals.call_count, 0)
        self.assertEqual(
            [
                sql
                for sql in trace
                if sql.split()[0] in {"BEGIN", "ROLLBACK", "COMMIT"}
            ],
            ["BEGIN IMMEDIATE", "ROLLBACK"],
        )
        self.unchanged(before, changes)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual((self.home / TOMBSTONE_FILE).read_bytes(), journal)

    def test_pending_precedes_current_policy_and_original_validation(
        self,
    ) -> None:
        self.pending()
        self.conn.execute("UPDATE review_control SET policy_json='{}'")
        self.path.unlink()
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        with (
            store.writer_lock(self.home),
            mock.patch.object(
                review_originals,
                "open_regular",
                side_effect=AssertionError("original read on pending intent"),
            ),
            self.assertRaisesRegex(ValueError, "^erasure_pending$") as caught,
        ):
            self.save(self.result([self.note()]))
        self.assertIs(type(caught.exception), ValueError)
        self.unchanged(before, changes)

    def test_receipt_precedes_pending_journal_policy_generation_and_source(
        self,
    ) -> None:
        with store.writer_lock(self.home):
            receipt = self.save(self.result([]))
        journal = self.pending()
        self.conn.execute(
            "UPDATE review_control SET policy_json='{}',"
            "generation=2,admission=0"
        )
        self.path.unlink()
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        with (
            store.writer_lock(self.home),
            mock.patch.object(
                tombstones,
                "read_intent",
                side_effect=AssertionError("journal read on receipt retry"),
            ),
            mock.patch.object(
                review_originals,
                "open_regular",
                side_effect=AssertionError("original read on receipt retry"),
            ),
        ):
            returned = self.save(self.result([self.note()]))
        self.assertEqual(returned, receipt)
        self.assertEqual(returned.no_change_reason, "No change.")
        self.unchanged(before, changes)
        self.assertEqual((self.home / TOMBSTONE_FILE).read_bytes(), journal)

    def test_quiescent_replay_keeps_affected_attempt_fenced(self) -> None:
        journal = self.pending()
        with review_quiescence.erase_connection(
            self.home, wait_s=0, status=review_quiescence.EraseStatus()
        ) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
            )
        self.assertEqual(
            tuple(
                self.row("SELECT state,current_attempt_id FROM review_request")
            ),
            ("cancelled", None),
        )
        self.assertEqual(
            self.row("SELECT outcome FROM review_attempt")[0],
            "evidence_erased",
        )
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        with (
            store.writer_lock(self.home),
            mock.patch.object(
                review_originals,
                "open_regular",
                side_effect=AssertionError("original read after erasure"),
            ),
            self.assertRaisesRegex(ValueError, "^rejected_fenced$"),
        ):
            self.save(self.result([self.note()]))
        self.unchanged(before, changes)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual((self.home / TOMBSTONE_FILE).read_bytes(), journal)
