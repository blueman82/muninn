"""Verify native consumer work shares one meter and closes its snapshots."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from muninn import (
    cursor_import,
    review_budget,
    review_evidence,
    review_originals,
    review_requests,
)
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import RequestView, ReviewContext
from muninn.review_progress import digest
from muninn.review_read_budget import ReadBudget
from tests import test_review_window_boundary as boundary_tests


class NativeConsumerReadBudgetTests(unittest.TestCase):
    """Use real unindexed synthetic native databases and immutable requests."""

    def setUp(self) -> None:
        self.boundary = boundary_tests.WindowBoundaryTests()
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        self.case = self.boundary.case
        self.conn = self.case.conn
        self.path, self.window, _ = self.boundary.native()
        self.selector = EvidenceSelector(
            json_pointer="/text", start=None, end=None
        )
        self.locator = EvidenceLocator(
            source=self.window.source,
            line=2,
            part=1,
            line_sha256=self.window.boundary_hash,
        )
        self.record = review_evidence.select_evidence(
            self.case.home, self.locator, self.selector
        )
        self.case.persist(self.record)
        self.policy = self.case.policy(self.record)
        self.conn.execute(
            "UPDATE review_control SET admission=1,generation=1,"
            "policy_json=?,policy_digest=?,activated_at=10.0",
            (canonical_json(self.policy), digest(self.policy)),
        )
        self.seen: list[tuple[ReadBudget, sqlite3.Connection, Path]] = []
        connect = review_originals._cursor_connection

        def observed(
            stack: ExitStack, path: Path, *, budget: ReadBudget
        ) -> sqlite3.Connection:
            conn = connect(stack, path, budget=budget)
            self.seen.append((budget, conn, path))
            return conn

        patch = mock.patch.object(
            review_originals, "_cursor_connection", side_effect=observed
        )
        patch.start()
        self.addCleanup(patch.stop)

    def transaction[T](self, action: Callable[[], T]) -> T:
        """Keep all writes in the caller's explicit transaction."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            self.conn.execute("COMMIT")
            return result
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def enqueue(self) -> RequestView:
        """Admit the native endpoint and its independently selected scope."""
        request = self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                self.window,
                self.record.scope,
                self.policy,
                "explicit",
                11.0,
            )
        )
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)",
            (request.id, self.record.id),
        )
        return request

    def checked_reads(self, count: int) -> ReadBudget:
        """Check identical accounts and closed snapshots after a root call."""
        self.assertEqual(len(self.seen), count)
        budget = self.seen[0][0]
        self.assertTrue(all(item[0] is budget for item in self.seen))
        self.assertLess(budget.sql_steps_left, 100_000)
        for _, conn, _ in self.seen:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        self.seen.clear()
        return budget

    def test_native_roots_share_connections_without_extra_reservation_reads(
        self,
    ) -> None:
        request = self.enqueue()
        grants = [self.checked_reads(1)]
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        grants.append(self.checked_reads(2))
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 13.0)
        )
        self.transaction(
            lambda: review_requests.retry_request(self.conn, request.id, 14.0)
        )
        grants.append(self.checked_reads(2))
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 15.0
            )
        )
        grants.append(self.checked_reads(2))
        context = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=self.record.scope,
            policy=self.policy,
            admitted_windows=(self.window,),
            evidence_ids=(self.record.id,),
            round_index=1,
            deadline=72.0,
        )
        for _ in range(2):
            self.transaction(
                lambda: review_budget.reserve_call(self.conn, context, 16.0)
            )
            grants.append(self.checked_reads(1))
        self.assertEqual(len({id(grant) for grant in grants}), 6)
        self.assertEqual(grants[0].bytes_left, grants[-1].bytes_left)
        self.assertEqual(grants[1].records_left, grants[0].records_left - 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_json,policy_digest FROM review_request"
            ).fetchone()[:],
            (canonical_json(self.policy), digest(self.policy)),
        )

    def test_claim_and_retry_share_native_steps_until_atomic_refusal(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                ((f"unrelated:{number}", b"{}") for number in range(5000)),
            )
        request = self.enqueue()
        boundary = self.checked_reads(1)
        review_evidence.validate_evidence(
            self.conn,
            self.case.home,
            (self.record.id,),
            self.record.scope,
            self.policy,
        )
        selected = self.checked_reads(1)
        self.assertGreater(boundary.sql_steps_left, 0)
        self.assertGreater(selected.sql_steps_left, 0)
        self.assertGreater(
            200_000 - boundary.sql_steps_left - selected.sql_steps_left,
            100_000,
        )
        for operation in ("claim", "retry"):
            if operation == "retry":
                self.transaction(
                    lambda: review_requests.cancel_request(
                        self.conn, request.id, 13.0
                    )
                )
            before = tuple(review_requests.request_row(self.conn, request.id))
            with self.assertRaisesRegex(ValueError, "evidence_limit"):
                if operation == "claim":
                    self.transaction(
                        lambda: review_requests.claim_attempt(
                            self.conn, request.id, 1, 14.0
                        )
                    )
                else:
                    self.transaction(
                        lambda: review_requests.retry_request(
                            self.conn, request.id, 14.0
                        )
                    )
            self.assertEqual(self.checked_reads(2).sql_steps_left, 0)
            self.assertEqual(
                tuple(review_requests.request_row(self.conn, request.id)),
                before,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM review_attempt"
                ).fetchone()[0],
                0,
            )

    def test_selected_evidence_across_databases_keeps_the_root_account(
        self,
    ) -> None:
        other = self.path.with_name("other.sqlite")
        other.write_bytes(self.path.read_bytes())
        with closing(sqlite3.connect(other)) as conn, conn:
            conn.execute(
                "UPDATE cursorDiskKV SET key=replace(key,':chat',':other')"
            )
        cursor_import.run(self.case.home, other, str(self.case.tmp), 0.0)
        record = review_evidence.select_evidence(
            self.case.home,
            self.locator.model_copy(
                update={
                    "source": SourceRef(provider="cursor", thread_id="other")
                }
            ),
            self.selector,
        )
        self.case.persist(record)
        self.seen.clear()
        request = self.enqueue()
        self.checked_reads(1)
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)", (request.id, record.id)
        )
        self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.assertEqual({item[2] for item in self.seen}, {self.path, other})
        self.assertEqual(self.checked_reads(3).records_left, 4087)
