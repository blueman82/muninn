"""Exercise operation-wide original-read grants through real queue paths."""

from __future__ import annotations

from collections.abc import Callable
from unittest import mock

from muninn import (
    review_budget,
    review_evidence,
    review_originals,
    review_requests,
)
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_json import canonical_json
from muninn.review_models import RequestView, ReviewContext
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import line
from tests.test_review_requests import RequestCase


class ConsumerReadBudgetTests(RequestCase):
    """Keep reads real while observing the transient grant passed to them."""

    def selected(self, request: RequestView) -> int:
        """Persist one selected original and attach it to the request."""
        window = self.window(2, 2)
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=window.source,
                line=2,
                part=1,
                line_sha256=window.boundary_hash,
            ),
            EvidenceSelector(
                json_pointer="/payload/content/0/text", start=None, end=None
            ),
        )
        data = record.model_dump(mode="json")
        for field in ("original_locator", "selector", "scope"):
            data[f"{field}_json"] = canonical_json(getattr(record, field))
            del data[field]
        self.conn.execute(
            f"INSERT OR IGNORE INTO review_evidence({','.join(data)}) "
            f"VALUES({','.join('?' for _ in data)})",
            tuple(data.values()),
        )
        ident = self.conn.execute(
            "SELECT id FROM review_evidence WHERE selector_digest=? "
            "AND line_sha256=?",
            (record.selector_digest, record.line_sha256),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)", (request.id, ident)
        )
        return len(line(self.records[1]))

    def cap(self, limit: int) -> None:
        """Install the same validated policy bytes in fixture control/rows."""
        self.policy = self.policy.model_copy(update={"max_input_bytes": limit})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def observe(self, action: Callable[[], object]) -> ReadBudget:
        """Observe one root's actual prefix and selected read accounts."""
        with (
            mock.patch.object(
                review_originals, "_jsonl", wraps=review_originals._jsonl
            ) as prefix,
            mock.patch.object(
                review_evidence, "reread", wraps=review_evidence.reread
            ) as selected,
        ):
            self.transaction(action)
        budgets = [
            call.kwargs["budget"]
            for call in prefix.call_args_list + selected.call_args_list
        ]
        self.assertTrue(budgets)
        self.assertTrue(all(budget is budgets[0] for budget in budgets))
        budget = budgets[0]
        self.assertEqual(
            budget.bytes_left,
            self.policy.max_input_bytes
            - len(self.path.read_bytes())
            - selected.call_count * len(line(self.records[1])),
        )
        self.assertEqual(budget.records_left, 4093 - selected.call_count)
        return budget

    def test_each_request_root_shares_reads_and_owns_a_fresh_grant(
        self,
    ) -> None:
        grants = [
            self.observe(
                lambda: review_requests.enqueue_review(
                    self.conn,
                    self.window(),
                    self.scope,
                    self.policy,
                    "explicit",
                    11.0,
                )
            )
        ]
        request = review_requests.request_view(
            self.conn.execute("SELECT * FROM review_request").fetchone()
        )
        self.selected(request)
        grants.append(
            self.observe(
                lambda: review_requests.claim_attempt(
                    self.conn, request.id, 1, 12.0
                )
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 13.0)
        )
        grants.append(
            self.observe(
                lambda: review_requests.retry_request(
                    self.conn, request.id, 14.0
                )
            )
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 15.0
            )
        )
        context = ReviewContext(
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(request.window,),
            evidence_ids=(),
            round_index=1,
            deadline=72.0,
        )
        grants.append(
            self.observe(
                lambda: review_budget.reserve_call(self.conn, context, 16.0)
            )
        )
        self.assertEqual(len({id(grant) for grant in grants}), 4)
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_json,policy_digest FROM review_request"
            ).fetchone()[:],
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def test_claim_and_retry_refuse_combined_bytes_then_accept_exact_fit(
        self,
    ) -> None:
        exact = len(self.path.read_bytes()) + len(line(self.records[1]))
        for cap in (exact - 1, exact):
            self.cap(cap)
            request = self.enqueue()
            self.selected(request)
            for operation in ("claim", "retry"):
                if operation == "retry":
                    self.transaction(
                        lambda r=request: review_requests.cancel_request(
                            self.conn, r.id, 13.0
                        )
                    )
                action = (
                    (
                        lambda r=request: review_requests.claim_attempt(
                            self.conn, r.id, 1, 14.0
                        )
                    )
                    if operation == "claim"
                    else (
                        lambda r=request: review_requests.retry_request(
                            self.conn, r.id, 14.0
                        )
                    )
                )
                before = tuple(
                    self.conn.execute(
                        "SELECT * FROM review_request WHERE id=?",
                        (request.id,),
                    ).fetchone()
                )
                evidence = self.conn.execute(
                    "SELECT evidence_id FROM review_input WHERE request_id=?",
                    (request.id,),
                ).fetchone()[0]
                review_evidence.validate_evidence(
                    self.conn, self.home, (evidence,), self.scope, self.policy
                )
                if cap == exact:
                    self.assertEqual(self.observe(action).bytes_left, 0)
                else:
                    with self.assertRaisesRegex(ValueError, "evidence_limit"):
                        self.transaction(action)
                    self.assertEqual(
                        tuple(
                            self.conn.execute(
                                "SELECT * FROM review_request WHERE id=?",
                                (request.id,),
                            ).fetchone()
                        ),
                        before,
                    )

    def test_supplied_enqueue_share_counts_failed_and_repeated_reads(
        self,
    ) -> None:
        size = len(self.path.read_bytes())
        budget = ReadBudget(size * 3)

        def enqueue(window_hash: str) -> RequestView:
            return self.transaction(
                lambda: review_requests.enqueue_review(
                    self.conn,
                    self.window().model_copy(
                        update={"boundary_hash": window_hash}
                    ),
                    self.scope,
                    self.policy,
                    "explicit",
                    11.0,
                    budget=budget,
                )
            )

        with self.assertRaisesRegex(ValueError, "evidence_changed"):
            enqueue("0" * 64)
        self.assertEqual(budget.bytes_left, size * 2)
        first = enqueue(self.window().boundary_hash)
        self.assertEqual(enqueue(self.window().boundary_hash).id, first.id)
        with self.assertRaisesRegex(ValueError, "evidence_limit"):
            enqueue(self.window().boundary_hash)
        self.assertEqual((budget.bytes_left, budget.records_left), (0, 4087))

    def test_reconciliation_all_windows_share_one_source_budget(self) -> None:
        self.cap(3 * len(self.path.read_bytes()))
        with mock.patch.object(
            review_originals, "_jsonl", wraps=review_originals._jsonl
        ) as prefix:
            ids = self.transaction(
                lambda: review_requests.reconcile_sources(
                    self.conn, self.policy, 12.0
                )
            )
        self.assertEqual(len(ids), 2)
        grants = [call.kwargs["budget"] for call in prefix.call_args_list]
        self.assertEqual(len(grants), 2)
        self.assertIs(grants[0], grants[1])
        self.assertEqual(grants[0].bytes_left, len(self.path.read_bytes()))
        self.assertEqual(
            self.conn.execute(
                "SELECT activation_line,completed_line FROM review_source"
            ).fetchone()[:],
            (0, 0),
        )

    def test_claim_aggregates_record_units_and_accepts_exact_fit(self) -> None:
        originals = list(self.records)
        for count in (4095, 4096):
            self.records = (
                originals[:2]
                + [{"type": "noop"}] * (count - 3)
                + originals[2:]
            )
            self.path.write_bytes(
                b"".join(line(record) for record in self.records)
            )
            self.run_ingest(full=True)
            self.cap(100_000)
            request = self.enqueue(self.window(2, count))
            self.selected(request)
            with (
                mock.patch.object(
                    review_originals, "_jsonl", wraps=review_originals._jsonl
                ) as prefix,
                mock.patch.object(
                    review_evidence, "reread", wraps=review_evidence.reread
                ) as selected,
            ):
                if count == 4095:
                    self.transaction(
                        lambda r=request: review_requests.claim_attempt(
                            self.conn, r.id, 1, 12.0
                        )
                    )
                    self.transaction(
                        lambda r=request: review_requests.cancel_request(
                            self.conn, r.id, 13.0
                        )
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "evidence_limit"):
                        self.transaction(
                            lambda r=request: review_requests.claim_attempt(
                                self.conn, r.id, 1, 12.0
                            )
                        )
                    self.assertEqual(
                        review_requests.request_row(self.conn, request.id)[
                            "state"
                        ],
                        "pending",
                    )
            budget = prefix.call_args.kwargs["budget"]
            self.assertIs(selected.call_args.kwargs["budget"], budget)
            self.assertEqual(budget.records_left, 0)

    def test_receipt_and_cancelled_duplicate_fast_paths_read_nothing(
        self,
    ) -> None:
        request = self.enqueue()
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 12.0)
        )
        with mock.patch.object(
            review_evidence, "originals", side_effect=AssertionError("read")
        ):
            self.assertEqual(self.enqueue().state, "cancelled")
        self.transaction(
            lambda: review_requests.retry_request(self.conn, request.id, 13.0)
        )
        self.receipt(request.id)
        with (
            mock.patch.object(
                review_evidence,
                "originals",
                side_effect=AssertionError("read"),
            ),
            mock.patch.object(
                review_evidence, "reread", side_effect=AssertionError("read")
            ),
        ):
            self.assertEqual(self.enqueue().state, "completed")
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, request.id, 1, 15.0
                )
            )
            self.transaction(
                lambda: review_requests.retry_request(
                    self.conn, request.id, 16.0
                )
            )
            self.transaction(
                lambda: review_requests.cancel_request(
                    self.conn, request.id, 17.0
                )
            )
