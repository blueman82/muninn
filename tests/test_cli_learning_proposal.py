"""Exercise real foreground proposal roots and intervening durable changes."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path
from unittest import mock

from muninn import knowledge_erase, review_budget, review_requests, store
from muninn.review_models import ReviewContext
from tests.test_cli_learning import CliLearningCase


class CliLearningProposalTests(CliLearningCase):
    """Retain real writer acquisition, business transactions and originals."""

    def between_roots(self, action: Callable[[], None]) -> Generator[None]:
        """Inject a durable action only after the actual first writer exits."""
        original = store.writer_lock
        roots = 0

        @contextlib.contextmanager
        def writer(home: Path, wait_s: float = 15.0) -> Generator[None]:
            nonlocal roots
            with original(home, wait_s=wait_s):
                roots += 1
                yield
            if roots == 1:
                with original(home, wait_s=0):
                    action()

        with mock.patch.object(store, "writer_lock", writer):
            yield

    def check_race(self, action: Callable[[], None]) -> None:
        """Require a winner between claim and commit to prevent new content."""
        with contextlib.contextmanager(self.between_roots)(action):
            code, out = self.propose()
        self.assertEqual((code, out.get("error")), (2, "rejected_fenced"))
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 2)

    def test_claim_root_releases_before_checked_commit(self) -> None:
        observations: list[tuple[str, int]] = []

        def observe() -> None:
            observations.append(
                tuple(
                    self.row(
                        "SELECT state,(SELECT count(*) FROM review_receipt) "
                        "FROM review_request"
                    )
                )
            )
            self.assertFalse(self.conn.in_transaction)
            with self.assertRaisesRegex(ValueError, "rejected_fenced"):
                self.save(self.result([]))

        with contextlib.contextmanager(self.between_roots)(observe):
            code, out = self.propose()
        self.assertEqual((code, out["outcome"]), (0, "completed"))
        self.assertEqual(observations, [("running", 0)])

    def test_intervening_cancel_wins(self) -> None:
        def cancel() -> None:
            self.transaction(
                lambda: review_requests.cancel_request(
                    self.conn, self.context.request_id, 21.0
                )
            )

        self.check_race(cancel)

    def test_intervening_erasure_wins(self) -> None:
        def erase() -> None:
            self.transaction(
                lambda: knowledge_erase.erase_review_support(
                    self.conn, self.context.evidence_ids, 21.0
                )
            )

        self.check_race(erase)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_evidence WHERE state='live'"
            )[0],
            0,
        )

    def test_intervening_generation_wins(self) -> None:
        def generation() -> None:
            self.conn.execute("UPDATE review_control SET generation=2")

        self.check_race(generation)
        self.assertEqual(
            self.row("SELECT generation FROM review_control")[0], 2
        )

    def test_failed_claim_rolls_back_old_worker_and_origin(self) -> None:
        self.conn.execute(
            "CREATE TRIGGER reject_local BEFORE INSERT ON review_attempt "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        before = tuple(self.conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.propose()
        self.assertEqual(tuple(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)

    def test_unknown_call_charge_and_worker_settings_are_retained(
        self,
    ) -> None:
        runtime = ReviewContext(
            request_id=self.context.request_id,
            attempt_id=self.context.attempt_id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(),),
            evidence_ids=self.context.evidence_ids,
            round_index=1,
            deadline=72.0,
        )
        self.transaction(
            lambda: review_budget.reserve_call(self.conn, runtime, 12.0)
        )
        before = dict(self.row("SELECT * FROM review_call"))
        control = tuple(self.row("SELECT * FROM review_control"))
        code, out = self.propose()
        self.assertEqual((code, out["outcome"]), (0, "completed"))
        after = dict(self.row("SELECT * FROM review_call"))
        self.assertEqual(after["state"], "unknown")
        for key in (
            "reserved_at",
            "cost_reservation_decimal",
            "requested_json",
            "budget_account_id",
            "policy_digest",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_control")), control
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 1)
        self.assertEqual(
            self.save(self.result([])).attempt_id, out["attempt_id"]
        )

    def test_receipt_first_survives_policy_and_original_loss(self) -> None:
        receipt = self.save(self.result([]))
        self.conn.execute("UPDATE review_control SET admission=0,generation=2")
        self.path.unlink()
        before = tuple(self.conn.iterdump())
        code, out = self.propose()
        self.assertEqual((code, out["attempt_id"]), (0, receipt.attempt_id))
        self.assertEqual(tuple(self.conn.iterdump()), before)
        self.assertEqual(
            self.row("SELECT origin FROM review_request")[0], "review"
        )
