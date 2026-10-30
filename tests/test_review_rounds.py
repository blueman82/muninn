"""Check the strict bounded reviewer protocol without evidence reads."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from muninn import review_requests, review_rounds
from muninn.review_models import ReviewContext, ReviewPolicy
from muninn.review_read_budget import ReadBudget
from tests import test_review_read_budget_native_consumers as native_tests
from tests import test_review_reads as read_tests
from tests.test_review_requests import RequestCase
from tests.test_review_shared_models import policy_data


def context() -> ReviewContext:
    """Build an explicit synthetic context with one admitted window."""
    return ReviewContext.model_validate_json(
        json.dumps(
            {
                "request_id": "R_" + "1" * 32,
                "attempt_id": "A_" + "2" * 32,
                "generation": 1,
                "scope": {"project_id": "project", "task": None},
                "policy": ReviewPolicy.model_validate(
                    policy_data()
                ).model_dump(),
                "admitted_windows": [
                    {
                        "source": {
                            "provider": "claude",
                            "thread_id": "synthetic",
                        },
                        "from_line": 2,
                        "through_line": 4,
                        "boundary_hash": "a" * 64,
                    }
                ],
                "evidence_ids": [],
                "round_index": 1,
                "deadline": 100.0,
            }
        )
    )


class ReviewRoundTests(unittest.TestCase):
    """Reject extra operations, widened scope and exhausted bounds."""

    def setUp(self) -> None:
        self.context = context()
        timer = mock.patch(
            "muninn.review_rounds.time.monotonic", return_value=10.0
        )
        timer.start()
        self.addCleanup(timer.stop)

    def parse(self, data: object) -> review_rounds.RoundRequest:
        """Parse independently constructed JSON through the public boundary."""
        return review_rounds.parse_round(
            json.dumps(data).encode(), self.context
        )

    def test_only_bounded_search_open_evidence_final(self) -> None:
        cases = [
            {
                "op": "search",
                "query": "decision",
                "scope": self.context.scope.model_dump(),
            },
            {"op": "open", "knowledge_id": "K_" + "3" * 32, "revision": None},
            self.evidence(),
            {
                "op": "final",
                "result": {
                    "schema_version": 1,
                    "changes": [],
                    "unresolved": [],
                    "no_change_reason": "No supported changes",
                },
            },
        ]
        for data in cases:
            with self.subTest(op=data["op"]):
                self.assertEqual(self.parse(data).op, data["op"])
        for data in [
            {"op": "shell", "command": "true"},
            [cases[0]],
            {**cases[0], "tools": []},
            {"op": "open", "knowledge_id": "K_" + "3" * 32, "revision": True},
        ]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.parse(data)

    def evidence(self) -> dict[str, object]:
        """Return a locator selected within the synthetic window."""
        return {
            "op": "evidence",
            "locator": {
                "source": {"provider": "claude", "thread_id": "synthetic"},
                "line": 3,
                "part": 1,
                "line_sha256": "b" * 64,
            },
            "selector": {
                "json_pointer": "/message/content",
                "start": None,
                "end": None,
            },
        }

    def test_round_byte_deadline_budget_caps(self) -> None:
        raw = json.dumps(
            {"op": "open", "knowledge_id": "K_" + "3" * 32, "revision": None}
        ).encode()
        exact = self.context.model_copy(
            update={
                "policy": self.context.policy.model_copy(
                    update={"max_output_bytes": len(raw)}
                )
            }
        )
        self.assertEqual(review_rounds.parse_round(raw, exact).op, "open")
        for changed in [
            exact.model_copy(
                update={
                    "policy": exact.policy.model_copy(
                        update={"max_output_bytes": len(raw) - 1}
                    )
                }
            ),
            self.context.model_copy(
                update={"round_index": self.context.policy.max_rounds + 1}
            ),
            self.context.model_copy(update={"deadline": 10.0}),
        ]:
            with self.subTest(context=changed), self.assertRaises(ValueError):
                review_rounds.parse_round(raw, changed)

    def test_evidence_read_outside_admitted_scope_refused(self) -> None:
        for line in (1, 5):
            data = self.evidence()
            data["locator"] = {
                "source": {"provider": "claude", "thread_id": "synthetic"},
                "line": line,
                "part": 1,
                "line_sha256": "b" * 64,
            }
            with self.assertRaisesRegex(ValueError, "rejected_scope"):
                self.parse(data)
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.parse(
                {
                    "op": "search",
                    "query": "x",
                    "scope": {"project_id": "other", "task": None},
                }
            )

    def test_duplicate_nonfinite_and_invalid_utf8_refused(self) -> None:
        for raw in [
            b'{"op":"open","op":"final"}',
            b'{"op":"open","revision":NaN}',
            b"\xff",
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                review_rounds.parse_round(raw, self.context)

    def test_final_is_locally_revalidated(self) -> None:
        with self.assertRaises(ValueError):
            self.parse(
                {
                    "op": "final",
                    "result": {
                        "schema_version": 1,
                        "changes": [],
                        "unresolved": [],
                        "no_change_reason": None,
                    },
                }
            )
        schema = review_rounds.round_schema()
        discriminator = schema["discriminator"]
        assert isinstance(discriminator, dict)
        self.assertEqual(discriminator["propertyName"], "op")
        self.assertEqual(
            set(discriminator["mapping"]),
            {"search", "open", "evidence", "final"},
        )

    def test_invalid_round_does_not_expose_response_text(self) -> None:
        with self.assertRaisesRegex(
            review_rounds.RoundFormatError, "^model_format$"
        ):
            self.parse({"op": "private invalid marker"})

    def test_json_conversion_and_recursion_failures_are_finite(self) -> None:
        admitted = self.context.model_copy(
            update={
                "policy": self.context.policy.model_copy(
                    update={"max_output_bytes": 20000}
                )
            }
        )
        for raw in (b"1" * 5000, b"[" * 5000 + b"0" + b"]" * 5000):
            with (
                self.subTest(size=len(raw)),
                self.assertRaisesRegex(ValueError, "^invalid_round$"),
            ):
                review_rounds.parse_round(raw, admitted)


class ReviewReadAdmissionTests(RequestCase):
    """Check direct evidence admission on disposable originals."""

    def test_evidence_content_follows_durable_admission(self) -> None:
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        admitted = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=request.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=100.0,
        )
        data = {
            "op": "evidence",
            "locator": {
                "source": request.window.source.model_dump(),
                "line": 2,
                "part": 1,
                "line_sha256": self.window(2, 2).boundary_hash,
            },
            "selector": {
                "json_pointer": "/payload/content/0/text",
                "start": None,
                "end": None,
            },
        }
        with mock.patch("time.monotonic", return_value=10.0):
            operation = review_rounds.parse_round(
                json.dumps(data).encode(), admitted
            )
            raw = review_rounds.execute_read(self.home, operation, admitted)
        record = json.loads(raw)
        linked = self.conn.execute(
            "SELECT evidence_id FROM review_input WHERE request_id=?",
            (request.id,),
        ).fetchall()
        self.assertEqual([row[0] for row in linked], [record["id"]])
        self.assertEqual(record["line"], 2)
        self.assertIsInstance(record["excerpt"], str)


class NativeReviewReadTests(unittest.TestCase):
    """Keep native snapshots and VM work shared across read phases."""

    def test_native_read_phases_share_one_allowance_and_close(self) -> None:
        case = native_tests.NativeConsumerReadBudgetTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        request = case.enqueue()
        attempt = case.transaction(
            lambda: review_requests.claim_attempt(
                case.conn, request.id, 1, 12.0
            )
        )
        admitted = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=request.scope,
            policy=case.policy,
            admitted_windows=(request.window,),
            evidence_ids=(case.record.id,),
            round_index=1,
            deadline=100.0,
        )
        operation = review_rounds.EvidenceRequest(
            op="evidence", locator=case.locator, selector=case.selector
        )
        case.seen.clear()
        with mock.patch("time.monotonic", return_value=10.0):
            raw = review_rounds.execute_read(
                case.case.home, operation, admitted
            )
        self.assertEqual(json.loads(raw)["id"], case.record.id)
        account = case.checked_reads(2)
        used = 100000 - account.sql_steps_left
        restricted = ReadBudget(case.policy.max_input_bytes)
        for _ in range(100000 - used + 1):
            restricted.sqlite_progress()
        with (
            mock.patch("time.monotonic", return_value=10.0),
            mock.patch(
                "muninn.review_rounds.ReadBudget", return_value=restricted
            ),
            self.assertRaises(ValueError),
        ):
            review_rounds.execute_read(case.case.home, operation, admitted)
        self.assertEqual(restricted.sql_steps_left, 0)
        self.assertGreater(len(case.seen), 1)
        case.checked_reads(len(case.seen))


class ReviewSupportBudgetTests(unittest.TestCase):
    """Aggregate physical reads across claim and independent expiry support."""

    def test_expiry_support_shares_bytes_records_and_admission(self) -> None:
        case = read_tests.ReviewReadTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.conn.execute(
            "UPDATE knowledge_version SET expiry_json=? WHERE knowledge_id=?",
            (
                json.dumps(
                    {
                        "at": 1000.0,
                        "condition": None,
                        "evidence_refs": [case.evidence[1].id],
                    }
                ),
                case.ident,
            ),
        )
        account = ReadBudget(case.policy.max_input_bytes)
        with mock.patch(
            "muninn.review_rounds.ReadBudget", return_value=account
        ):
            case.read()
        self.assertEqual(
            set(case.links()), {item.id for item in case.evidence}
        )
        used_bytes = case.policy.max_input_bytes - account.bytes_left
        used_records = 4096 - account.records_left
        self.assertGreater(used_records, 4)
        for dimension in ("bytes", "records"):
            case.policy_limit(policy_id="bounded-" + dimension)
            restricted = ReadBudget(case.policy.max_input_bytes)
            if dimension == "bytes":
                restricted.consume_bytes(
                    case.policy.max_input_bytes - used_bytes + 1
                )
            else:
                for _ in range(4096 - used_records + 1):
                    restricted.consume_record()
            with (
                self.subTest(dimension=dimension),
                mock.patch(
                    "muninn.review_rounds.ReadBudget", return_value=restricted
                ),
                self.assertRaises(ValueError),
            ):
                case.read()
            self.assertEqual(case.links(), ())
        case.policy_limit(policy_id="expired")
        with (
            mock.patch("time.time", return_value=1000.0),
            self.assertRaisesRegex(ValueError, "knowledge_ineligible"),
        ):
            case.read()
        self.assertEqual(case.links(), ())
