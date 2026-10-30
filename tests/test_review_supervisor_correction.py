"""Exercise one bounded correction through disposable native wrappers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from unittest import mock

from muninn import review_supervisor_result as completion
from muninn import store
from muninn.review_json import canonical_json
from muninn.review_round_state import Round
from tests.test_review_supervisor import SupervisorCase


class ReviewSupervisorCorrectionTests(SupervisorCase):
    """Catch correction escapes from existing call and authority bounds."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = self.policy.model_copy(
            update={
                "max_attempts": 3,
                "retry_backoff_seconds": [5.0, 30.0],
                "concurrency": 4,
                "budget": self.policy.budget.model_copy(
                    update={
                        "max_model_calls": 10,
                        "max_cost_decimal": "10",
                        "conservative_call_cost_decimal": "0.1",
                        "currency": "USD",
                    }
                ),
            }
        )
        self.bind_policy()

    def bind_policy(self) -> None:
        """Bind a disposable policy consistently before claiming requests."""
        self.supervisor.stop(12.0)
        self.supervisor.policy = self.policy
        self.conn.execute("DELETE FROM review_request")
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.request = self.enqueue()
        self.supervisor.start(12.0)

    def runner(self, first: str, second: str) -> None:
        """Emit two chosen native results and retain only fixture prompts."""
        self.script = (
            "import json,sys; from pathlib import Path; "
            "p=json.load(sys.stdin); d=p['data']; n=d['round_index']; "
            f"Path({str(self.home)!r},d['request_id']+'-'+str(n))"
            ".write_text(json.dumps(p)); "
            f"op=({first}) if n==1 else ({second}); "
            "print(json.dumps({'type':'result','subtype':'success',"
            "'is_error':False,'structured_output':op,"
            "'usage':{'input_tokens':7,'output_tokens':3},"
            "'total_cost_usd':0.02}))"
        )

    def final(self, changes: str = "[]") -> str:
        """Return a literal final operation for the disposable runner."""
        return (
            "{'op':'final','result':{'schema_version':1,'changes':"
            + changes
            + ",'unresolved':[],'no_change_reason':"
            + (
                "'No supported candidates remain after correction.'"
                if changes == "[]"
                else "None"
            )
            + "}}"
        )

    def note(self, scope: str = "d['scope']") -> str:
        """Build a candidate using evidence actually admitted to the runner."""
        return (
            "{'action':'add','form':'note','kind':'fact','scope':"
            + scope
            + ",'content':'Keep this fact.','conditions':[],"
            "'evidence_refs':[d['evidence_ids'][0]],'reason':'Supported.'}"
        )

    def drive(self, predicate: Callable[[], bool], now: float = 13.0) -> None:
        """Drive real process events to a bounded durable test boundary."""
        until = time.monotonic() + 8
        while time.monotonic() < until:
            self.supervisor.tick(now)
            self.assertLessEqual(len(self.supervisor._rounds), 4)
            if predicate():
                return
            time.sleep(0.01)
        self.fail("no durable boundary")

    def terminal(self) -> bool:
        """Report whether the tested attempt has a durable disposition."""
        return bool(
            self.conn.execute(
                "SELECT 1 FROM review_attempt WHERE ended_at IS NOT NULL"
            ).fetchone()
        )

    def prompt(self, index: int) -> dict[str, Any]:
        """Read the fake provider's own prompt capture."""
        return json.loads(
            (self.home / f"{self.request.id}-{index}").read_text()
        )["data"]

    def second_boundary(self, action: Callable[[Round], None]) -> Any:
        """Inspect the durable first call before a real second launch."""
        spawn = self.supervisor._spawn

        def inspect(pending: Round, now: float) -> None:
            if pending.context.round_index == 2:
                with store.writer_lock(self.home, wait_s=0):
                    action(pending)
            spawn(pending, now)

        return mock.patch.object(
            self.supervisor, "_spawn", side_effect=inspect
        )

    def test_format_then_final_uses_same_attempt(self) -> None:
        self.runner("{'op':'final','raw':'DO-NOT-RETAIN'}", self.final())
        seen: list[tuple[str, str, float, object]] = []
        launch = self.launch

        def capture(*args: Any) -> Any:
            context = args[0]
            seen.append(
                (
                    context.request_id,
                    context.attempt_id,
                    context.deadline,
                    context.scope,
                )
            )
            return launch(*args)

        with mock.patch(
            "muninn.review_supervisor.prepare_launch", side_effect=capture
        ):
            self.finish()
        self.assertEqual(len(set(seen)), 1)
        first, second = self.prompt(1), self.prompt(2)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["scope"], second["scope"])
        self.assertEqual(second["history"], [])
        self.assertEqual(
            second["correction"],
            {
                "kind": "format",
                "code": "model_format",
                "collection": None,
                "index": None,
            },
        )
        self.assertNotIn("DO-NOT-RETAIN", json.dumps(second))
        self.assertNotIn("DO-NOT-RETAIN", "\n".join(self.conn.iterdump()))
        calls = self.conn.execute(
            "SELECT * FROM review_call ORDER BY round_index"
        ).fetchall()
        self.assertEqual(
            [row["outcome"] for row in calls],
            ["correction_format_requested", "completed"],
        )
        self.assertEqual([row["state"] for row in calls], ["completed"] * 2)
        self.assertEqual([row["ended_at"] for row in calls], [13.0] * 2)
        self.assertEqual(
            sum(Decimal(row["cost_reservation_decimal"]) for row in calls),
            Decimal("0.2"),
        )
        self.assertEqual(
            [json.loads(row["usage_json"])["input_tokens"] for row in calls],
            [7, 7],
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM review_attempt"),
            1,
        )

    def test_reduction_then_final_has_no_partial_write(self) -> None:
        good, bad = self.note(), self.note(
            "{'project_id':'wrong','task':None}"
        )
        self.runner(self.final(f"[{good},{bad}]"), self.final(f"[{good}]"))
        observed: list[str] = []

        def inspect(pending: Round) -> None:
            self.assertEqual(
                self.conn.execute("SELECT count(*) FROM knowledge").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                self.scalar("SELECT count(*) FROM review_receipt"),
                0,
            )
            observed.append(pending.context.attempt_id)

        with self.second_boundary(inspect):
            self.finish()
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM knowledge"),
            1,
        )
        self.assertEqual(
            self.prompt(2)["correction"],
            {
                "kind": "reduction",
                "code": "candidate_scope",
                "collection": "changes",
                "index": 1,
            },
        )
        self.assertEqual(self.prompt(2)["history"], [])
        self.assertNotIn("Keep this fact.", json.dumps(self.prompt(2)))

    def test_corrective_read_and_second_correction_are_refused(self) -> None:
        self.runner(
            "{'op':'final'}", "{'op':'search','query':'x','scope':d['scope']}"
        )
        self.drive(self.terminal)
        self.assertTrue((self.home / f"{self.request.id}-2").exists())
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM review_receipt"),
            0,
        )
        self.assertEqual(
            self.scalar("SELECT error_code FROM review_attempt"),
            "rejected_validation",
        )

    def test_second_format_failure_has_no_third_call(self) -> None:
        self.runner("{'op':'final'}", "{'op':'final'}")
        self.drive(self.terminal)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )

    def test_changed_original_after_deferred_failure_is_terminal(self) -> None:
        bad = self.note("{'project_id':'wrong','task':None}")
        self.runner(self.final(f"[{bad}]"), self.final())

        def change(pending: Round) -> None:
            self.path.write_bytes(b"{}\n")

        with self.second_boundary(change):
            self.drive(self.terminal)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )
        self.assertFalse((self.home / f"{self.request.id}-2").exists())

    def test_correction_never_exceeds_concurrency_or_budget(self) -> None:
        self.policy = self.policy.model_copy(
            update={
                "budget": self.policy.budget.model_copy(
                    update={"max_model_calls": 4}
                )
            }
        )
        self.bind_policy()
        for first, last in ((1, 2), (1, 3), (2, 2), (3, 3)):
            self.enqueue(self.window(first, last))
        self.runner("{'op':'final'}", self.final())
        self.supervisor.tick(13.0)
        self.assertEqual(len(self.supervisor._rounds), 4)
        self.drive(
            lambda: self.scalar(
                "SELECT count(*) FROM review_attempt WHERE ended_at IS NULL"
            )
            == 0
            and not self.supervisor._rounds
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            4,
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM review_receipt"),
            0,
        )

    def test_first_read_leaves_no_correction_round(self) -> None:
        self.runner(
            "{'op':'search','query':'x','scope':d['scope']}", "{'op':'final'}"
        )
        self.drive(self.terminal)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )
        self.assertEqual(len(self.prompt(2)["history"]), 1)
        self.assertNotIn("correction", self.prompt(2))

    def test_native_failure_does_not_request_correction(self) -> None:
        self.script = "print('invalid native output')"
        self.drive(self.terminal)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM review_receipt"),
            0,
        )

    def marker_refusal(self, update: str) -> None:
        """Reject a marker after a durable boundary changes under it."""
        self.runner("{'op':'final'}", self.final())
        persist = self.supervisor._persist

        def remove(event: Any, now: float) -> None:
            persist(event, now)
            if event.kind == "outcome":
                self.conn.execute(update)

        with mock.patch.object(
            self.supervisor, "_persist", side_effect=remove
        ):
            self.drive(self.terminal)
        self.assertEqual(
            self.scalar("SELECT error_code FROM review_attempt"),
            "rejected_fenced",
        )
        self.assertFalse((self.home / f"{self.request.id}-2").exists())

    def test_missing_marker_target_fences_correction(self) -> None:
        self.marker_refusal("DELETE FROM review_call")

    def test_noncompleted_marker_target_fences_correction(self) -> None:
        self.marker_refusal("UPDATE review_call SET state='unknown'")

    def test_reduction_cannot_rewrite_a_retained_candidate(self) -> None:
        good = self.note()
        bad = self.note("{'project_id':'wrong','task':None}")
        changed = good.replace("Keep this fact.", "A different fact.")
        self.runner(self.final(f"[{good},{bad}]"), self.final(f"[{changed}]"))
        self.drive(self.terminal)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM knowledge"),
            0,
        )
        self.assertEqual(
            self.scalar("SELECT error_code FROM review_attempt"),
            "rejected_validation",
        )

    def test_policy_fence_between_refresh_and_marker_is_terminal(self) -> None:
        self.runner("{'op':'final'}", self.final())
        mark = completion.mark_correction

        def changed(conn: Any, context: Any, kind: Any) -> None:
            conn.execute(
                "UPDATE review_control SET policy_digest=?", ("f" * 64,)
            )
            mark(conn, context, kind)

        with mock.patch.object(
            completion, "mark_correction", side_effect=changed
        ):
            self.drive(self.terminal)
        self.assertFalse((self.home / f"{self.request.id}-2").exists())
        self.assertEqual(
            self.scalar("SELECT error_code FROM review_attempt"),
            "rejected_fenced",
        )

    def scalar(self, sql: str) -> Any:
        """Read one scalar from the disposable database."""
        return self.conn.execute(sql).fetchone()[0]

    def test_runner_failures_keep_three_attempts_and_both_backoffs(
        self,
    ) -> None:
        self.script = "raise SystemExit(1)"
        for count, now, retry in (
            (1, 13.0, 18.0),
            (2, 18.0, 48.0),
            (3, 48.0, None),
        ):
            self.drive(
                lambda expected=count: self.scalar(
                    "SELECT count(*) FROM review_attempt "
                    "WHERE ended_at IS NOT NULL"
                )
                == expected,
                now,
            )
            self.assertEqual(
                self.scalar("SELECT retry_at FROM review_request"), retry
            )
            self.assertEqual(
                self.scalar("SELECT count(*) FROM review_call"), count
            )
            self.assertEqual(
                self.scalar("SELECT max(round_index) FROM review_call"), 1
            )
        self.assertEqual(
            self.scalar("SELECT state FROM review_request"), "failed"
        )
        self.assertEqual(
            self.scalar("SELECT stage FROM review_issue"), "runner"
        )
