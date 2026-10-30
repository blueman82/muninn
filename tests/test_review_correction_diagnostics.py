"""Require durable call evidence for correction history and safe phases."""

from __future__ import annotations

import json
import sqlite3
import time
from unittest import mock

from muninn import (
    review_commit,
    review_diagnostics,
    review_recovery,
    review_supervisor_result,
)
from muninn.cli_learn_explain import explain
from muninn.review_models import ReviewContext
from muninn.review_process import ProcessOutcome
from muninn.review_read_budget import EvidenceLimitError
from muninn.review_requests import UNKNOWN_CONFIG
from muninn.review_round_state import Round
from muninn.review_supervisor import Supervisor
from muninn.review_supervisor_store import mark_correction
from tests.test_review_commit import CommitCase

SECRET = "private-malformed-proposal-and-source-canary"


class CorrectionDiagnosticsTests(CommitCase):
    """Reject guesses from call counts, other rounds, and later attempts."""

    def status(self) -> str:
        """Read the implemented projection without hiding a missing API."""
        return review_diagnostics.correction_status(
            self.conn, self.context.request_id
        )

    def call_row(
        self,
        index: int,
        *,
        attempt: str | None = None,
        outcome: str = "completed",
        launched: bool = True,
        completed: bool = True,
    ) -> None:
        """Store explicit synthetic lifecycle evidence without a provider."""
        self.conn.execute(
            "INSERT INTO review_call(attempt_id,round_index,"
            "budget_account_id,policy_digest,state,reserved_at,launched_at,"
            "ended_at,requested_json,observed_json,outcome) "
            "VALUES(?,?, 'test',?, ?,12,?,?, '{}','{}',?)",
            (
                attempt or self.context.attempt_id,
                index,
                self.digest(self.policy),
                (
                    "completed"
                    if completed
                    else "launched" if launched else "reserved"
                ),
                12.5 if launched else None,
                13.0 if completed else None,
                outcome,
            ),
        )

    def marker(self) -> None:
        """Store the first round's durable correction request marker."""
        self.call_row(1, outcome="correction_format_requested")

    def test_correction_history_requires_marker_and_launch(self) -> None:
        self.assertEqual(self.status(), "unknown")
        self.marker()
        self.assertEqual(self.status(), "requested")
        self.call_row(2, launched=False, completed=False)
        self.assertEqual(self.status(), "requested")
        self.conn.execute(
            "UPDATE review_call SET state='completed',ended_at=13 "
            "WHERE round_index=2"
        )
        self.assertEqual(self.status(), "requested")
        self.conn.execute(
            "UPDATE review_call SET "
            "launched_at=12.5,state='launched',ended_at=NULL WHERE "
            "round_index=2"
        )
        self.assertEqual(self.status(), "attempted")
        self.conn.execute(
            "UPDATE review_call SET state='completed',ended_at=13 "
            "WHERE round_index=2"
        )
        self.assertEqual(self.status(), "attempted")
        self.save(self.result([]))
        self.assertEqual(self.status(), "succeeded")

    def test_completed_successor_requires_same_attempt_failure(self) -> None:
        self.marker()
        self.call_row(2)
        self.conn.execute("UPDATE review_request SET state='failed'")
        self.assertEqual(self.status(), "attempted")
        self.conn.execute(
            "UPDATE review_attempt SET "
            "ended_at=14,error_code='rejected_validation'"
        )
        self.assertEqual(self.status(), "exhausted")

    def test_later_attempt_receipt_does_not_prove_correction(self) -> None:
        self.marker()
        self.call_row(2)
        old = self.context.attempt_id
        self.conn.execute(
            "UPDATE review_attempt SET "
            "ended_at=13,error_code='rejected_validation'"
        )
        newer = "A_" + "f" * 32
        self.conn.execute(
            (
                "INSERT INTO "
                "review_attempt(id,request_id,generation,observed_json,"
                "started_at,progress_at,ended_at,outcome) "
                "VALUES(?,?,1,'{}',14,15,15,'no_change')"
            ),
            (newer, self.context.request_id),
        )
        self.conn.execute(
            (
                "INSERT INTO review_receipt "
                "VALUES(?,?,'[]','[]',15,'no_change','No change')"
            ),
            (self.context.request_id, newer),
        )
        self.assertNotEqual(old, newer)
        self.assertEqual(self.status(), "exhausted")
        self.conn.execute(
            "UPDATE review_attempt SET error_code=NULL WHERE id=?", (old,)
        )
        self.assertEqual(self.status(), "attempted")

    def test_round_gaps_and_unmarked_read_final_are_not_corrections(
        self,
    ) -> None:
        self.call_row(1)
        self.call_row(2)
        self.save(self.result([]))
        self.assertEqual(self.status(), "unknown")
        self.conn.execute(
            "UPDATE review_call SET "
            "outcome='correction_reduction_requested' WHERE "
            "round_index=1"
        )
        self.conn.execute(
            "UPDATE review_call SET round_index=3 WHERE round_index=2"
        )
        self.assertEqual(self.status(), "requested")
        self.conn.execute(
            "UPDATE review_call SET state='unknown' WHERE round_index=1"
        )
        self.assertEqual(self.status(), "unknown")

    def test_latest_marked_attempt_orders_by_start_then_id(self) -> None:
        self.marker()
        self.call_row(2)
        self.conn.execute(
            "UPDATE review_attempt SET "
            "ended_at=13,error_code='rejected_validation'"
        )
        for suffix, started, marked in (
            ("e", 14, True),
            ("f", 14, True),
            ("d", 15, False),
        ):
            attempt = "A_" + suffix * 32
            self.conn.execute(
                (
                    "INSERT INTO "
                    "review_attempt(id,request_id,generation,observed_json,"
                    "started_at,progress_at) VALUES(?,?,1,'{}',?,?)"
                ),
                (attempt, self.context.request_id, started, started),
            )
            self.call_row(
                1,
                attempt=attempt,
                outcome=(
                    "correction_format_requested" if marked else "completed"
                ),
            )
            if suffix == "e":
                self.call_row(2, attempt=attempt)
        self.assertEqual(self.status(), "requested")
        self.conn.execute(
            "DELETE FROM review_call WHERE attempt_id=?", ("A_" + "f" * 32,)
        )
        self.assertEqual(self.status(), "attempted")

    def test_commit_errors_record_the_operation_boundary(self) -> None:
        for kind, expected in (
            ("candidate", "candidate validation"),
            ("original", "original evidence"),
            ("commit", "commit"),
        ):
            with self.subTest(kind=kind):
                result = (
                    self.result([self.note(evidence_refs=["E_" + "f" * 32])])
                    if kind == "candidate"
                    else self.result([])
                )
                if kind == "original":
                    self.path.write_bytes(b"{}\n")
                if kind == "commit":
                    self.conn.execute("UPDATE review_control SET generation=2")
                with self.assertRaises(
                    review_commit.CommitRejectedError
                ) as caught:
                    self.save(result)
                self.assertEqual(
                    getattr(caught.exception, "phase", None), expected
                )
                self.conn.execute(
                    "UPDATE review_attempt SET "
                    "ended_at=NULL,error_code=NULL,outcome=NULL"
                )
                self.conn.execute(
                    "UPDATE review_request SET state='running',"
                    "current_attempt_id=?",
                    (self.context.attempt_id,),
                )
                self.conn.execute("UPDATE review_control SET admission=1")

    def test_diagnostic_text_never_contains_rejected_payload(self) -> None:
        self.conn.execute("UPDATE review_evidence SET excerpt=?", (SECRET,))
        self.transaction(
            lambda: review_recovery.record(
                self.conn,
                self.context.request_id,
                "rejected_validation",
                "model format",
                14.0,
            )
        )
        rendered = json.dumps(explain(self.conn, self.context.request_id))
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn(
            SECRET,
            review_supervisor_result.failure_code(
                ValueError("rejected_" + SECRET), "rejected_validation"
            ),
        )

    def test_evidence_limit_code_survives_with_original_phase(self) -> None:
        with (
            mock.patch(
                "muninn.review_commit.validate_evidence",
                side_effect=EvidenceLimitError("evidence_record_limit"),
            ),
            self.assertRaises(review_commit.CommitRejectedError) as caught,
        ):
            self.save(self.result([]))
        self.assertEqual(str(caught.exception), "evidence_record_limit")
        self.assertEqual(
            getattr(caught.exception, "phase", None), "original evidence"
        )

    def pending(self, index: int = 2) -> Round:
        """Build an actual admitted context with no provider execution."""
        return Round(
            context=ReviewContext(
                **self.context.model_dump(exclude={"home", "now"}),
                scope=self.scope,
                policy=self.policy,
                admitted_windows=(self.window(),),
                round_index=index,
                deadline=time.monotonic() + 60,
            ),
            initial=None,
            knowledge=None,
        )

    def reconcile(
        self, raw: bytes, error: str | None = None, index: int = 2
    ) -> None:
        """Run actual result handling and persist its safe observed phase."""
        pending = self.pending(index)
        outcome = ProcessOutcome(
            attempt_id=self.context.attempt_id,
            round_index=index,
            stdout=raw,
            exit_code=0,
            error_code=error,
            elapsed_seconds=0.1,
            observed=UNKNOWN_CONFIG,
            usage=None,
        )

        def fail(
            context: ReviewContext, code: str, now: float, stage: str
        ) -> None:
            self.transaction(
                lambda: review_recovery.record(
                    self.conn, context.request_id, code, stage, now
                )
            )

        review_supervisor_result.execute(
            self.home, pending, outcome, 14.0, fail
        )

    def phase(self) -> str:
        """Read the notice written by actual result reconciliation."""
        return self.row("SELECT stage FROM review_issue")[0]

    def test_native_failure_is_not_a_model_format_failure(self) -> None:
        self.reconcile(SECRET.encode(), "invalid_native_output")
        self.assertEqual(self.phase(), "native output")
        self.assertEqual(self.status(), "unknown")
        self.assertNotIn(
            SECRET, json.dumps(explain(self.conn, self.context.request_id))
        )

    def test_malformed_model_result_reports_model_format(self) -> None:
        self.reconcile(json.dumps({"op": "final", "raw": SECRET}).encode())
        self.assertEqual(self.phase(), "model format")
        self.assertNotIn(SECRET, "\n".join(self.conn.iterdump()))

    def test_candidate_failure_reports_candidate_boundary(self) -> None:
        raw = {
            "op": "final",
            "result": self.result(
                [self.note(scope={"project_id": "other", "task": None})]
            ).model_dump(),
        }
        self.reconcile(json.dumps(raw).encode())
        self.assertEqual(self.phase(), "candidate validation")

    def test_original_failure_reports_original_boundary(self) -> None:
        self.path.write_text(
            self.path.read_text().replace("Use Python", SECRET)
        )
        raw = {"op": "final", "result": self.result([]).model_dump()}
        self.reconcile(json.dumps(raw).encode())
        self.assertEqual(self.phase(), "original evidence")
        self.assertNotIn(
            SECRET, json.dumps(explain(self.conn, self.context.request_id))
        )

    def test_controlled_read_failure_reports_read_boundary(self) -> None:
        self.path.write_bytes(b"{}\n")
        raw = {
            "op": "search",
            "query": "safe",
            "scope": self.scope.model_dump(),
        }
        self.reconcile(json.dumps(raw).encode())
        self.assertEqual(self.phase(), "controlled read")

    def test_marker_changes_only_outcome_metadata(self) -> None:
        self.call_row(1)
        before = dict(self.row("SELECT * FROM review_call"))
        self.transaction(
            lambda: mark_correction(
                self.conn, self.pending(1).context, "format"
            )
        )
        after = dict(self.row("SELECT * FROM review_call"))
        self.assertEqual(after.pop("outcome"), "correction_format_requested")
        before.pop("outcome")
        self.assertEqual(after, before)

    def test_fence_between_completed_call_and_marker_writes_no_marker(
        self,
    ) -> None:
        self.call_row(1)
        mark = review_supervisor_result.mark_correction

        def fenced(
            conn: sqlite3.Connection, context: ReviewContext, kind: str
        ) -> None:
            conn.execute("UPDATE review_control SET generation=generation+1")
            mark(conn, context, "format")

        with mock.patch.object(
            review_supervisor_result, "mark_correction", side_effect=fenced
        ):
            self.reconcile(b'{"op":"final"}', index=1)
        self.assertEqual(
            self.row("SELECT outcome FROM review_call")[0], "completed"
        )
        self.assertEqual(self.status(), "unknown")
        self.assertEqual(self.phase(), "commit")

    def test_native_and_unknown_process_outcomes_preserve_call_metadata(
        self,
    ) -> None:
        self.call_row(1)
        self.conn.execute(
            "UPDATE review_call SET state='unknown',"
            "outcome='native_tool_event'"
        )
        before = dict(self.row("SELECT * FROM review_call"))
        pending = self.pending(1)
        supervisor = Supervisor(self.home, self.policy)
        supervisor._rounds[self.context.attempt_id, 1] = pending
        outcome = ProcessOutcome(
            attempt_id=self.context.attempt_id,
            round_index=1,
            stdout=b'{"op":"final"}',
            exit_code=0,
            error_code="native_tool_event",
            elapsed_seconds=0.1,
            observed=UNKNOWN_CONFIG,
            usage=None,
        )
        supervisor._event(outcome, 14.0)
        self.assertEqual(dict(self.row("SELECT * FROM review_call")), before)
        self.assertEqual(self.status(), "unknown")
        self.assertEqual(supervisor._next, [])

    def test_successor_in_another_attempt_is_not_correction_launch(
        self,
    ) -> None:
        self.marker()
        other = "A_" + "f" * 32
        self.conn.execute(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "observed_json,started_at,progress_at) "
            "VALUES(?,?,1,'{}',14,14)",
            (other, self.context.request_id),
        )
        self.call_row(2, attempt=other)
        self.assertEqual(self.status(), "requested")
