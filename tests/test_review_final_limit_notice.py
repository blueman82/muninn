"""Keep final verification allowances intact through supervisor recovery."""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from contextlib import closing

from muninn import (
    review_evidence,
    review_recovery,
    review_requests,
    review_supervisor_result,
)
from muninn.review_claims import UNKNOWN_CONFIG
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_models import ReviewContext
from muninn.review_process import ProcessOutcome
from muninn.review_round_state import Round
from muninn.review_supervisor import Supervisor
from tests import test_review_commit_budget as budget_tests
from tests.ingest_support import line


class FinalLimitNoticeTests(unittest.TestCase):
    """Run real failed commits through the supervisor's durable notice path."""

    def setUp(self) -> None:
        self.case = budget_tests.ReviewCommitBudgetTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def reconcile(self, expected: str) -> None:
        """Reconcile a real final proposal and inspect its saved notice."""
        case = self.case
        admitted = review_requests.request_view(
            case.row(
                "SELECT * FROM review_request WHERE id=?",
                (case.context.request_id,),
            )
        ).window
        context = ReviewContext(
            request_id=case.context.request_id,
            attempt_id=case.context.attempt_id,
            generation=case.context.generation,
            scope=case.scope,
            policy=case.policy,
            admitted_windows=(admitted,),
            evidence_ids=case.context.evidence_ids,
            round_index=1,
            deadline=time.monotonic() + 60,
        )
        pending = Round(context, {}, {})
        outcome = ProcessOutcome(
            attempt_id=context.attempt_id,
            round_index=1,
            stdout=json.dumps(
                {
                    "op": "final",
                    "result": case.result([case.note()]).model_dump(),
                }
            ).encode(),
            exit_code=0,
            error_code=None,
            elapsed_seconds=0.0,
            observed=UNKNOWN_CONFIG,
            usage=None,
        )
        supervisor = object.__new__(Supervisor)
        supervisor.home = case.home
        supervisor.policy = case.policy
        self.assertFalse(
            review_supervisor_result.execute(
                case.home, pending, outcome, 17.0, supervisor._fail
            )
        )
        self.assertEqual(
            case.row(
                "SELECT error_code FROM review_attempt WHERE id=?",
                (context.attempt_id,),
            )[0],
            expected,
        )
        issue = review_recovery.notice(case.conn)
        assert issue is not None
        self.assertEqual(issue["code"], expected)
        self.assertIn("Do not retry", issue["action"])
        self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_final_byte_limit_notice_matches_durable_attempt(self) -> None:
        case = self.case
        exact = len(case.path.read_bytes()) + sum(
            len(line(r)) for r in case.records[1:]
        )
        case.claim_inputs(case.window(), exact - 1)
        self.reconcile("evidence_byte_limit")

    def test_final_record_limit_notice_matches_durable_attempt(self) -> None:
        case = self.case
        case.evidence = case.evidence[:1]
        case.records = case.records[:2] + [{"type": "noop"}] * 4094
        case.path.write_bytes(b"".join(line(r) for r in case.records))
        case.run_ingest(full=True)
        case.claim_inputs(case.window(2, 4096), 100_000)
        self.reconcile("evidence_record_limit")

    def test_final_native_work_notice_matches_durable_attempt(self) -> None:
        case = self.case
        path, _ = case.native()
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                ((f"unrelated:{i}", b"{}") for i in range(7200)),
            )
        self.reconcile("evidence_work_limit")

    def test_final_selection_notice_includes_accumulated_metadata(
        self,
    ) -> None:
        case = self.case
        window = case.window(2, 2)
        case.evidence = tuple(
            review_evidence.select_evidence(
                case.home,
                EvidenceLocator(
                    source=window.source,
                    line=2,
                    part=1,
                    line_sha256=window.boundary_hash,
                ),
                EvidenceSelector(
                    json_pointer="/payload/content/0/text", start=0, end=end
                ),
            )
            for end in range(1, 9)
        )
        for record in case.evidence:
            case.persist(record)
        self.assertLess(sum(len(r.excerpt or "") for r in case.evidence), 100)
        case.claim_inputs(window, 4096)
        self.reconcile("evidence_selection_limit")
