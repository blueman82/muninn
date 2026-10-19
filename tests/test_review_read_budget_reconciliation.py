"""Prove batch grants remain disjoint across real source SAVEPOINTs."""

from __future__ import annotations

import os
from unittest import mock

from muninn import (
    review_evidence,
    review_originals,
    review_progress,
    review_requests,
)
from muninn.review_json import canonical_json
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import TID, rollout
from tests.test_classify import codex_meta, user_msg
from tests.test_review_requests import RequestCase


def counters(budget: ReadBudget) -> tuple[int, int, int]:
    """Observe the three public remaining allowances without changing them."""
    return budget.bytes_left, budget.records_left, budget.sql_steps_left


class ReconciliationReadBudgetTests(RequestCase):
    """Observe real partition grants and reads without replacing their work."""

    def setUp(self) -> None:
        super().setUp()
        self.allocations: list[tuple[ReadBudget, tuple[ReadBudget, ...]]] = []
        self.grants: list[tuple[int, int, int]] = []
        partition = ReadBudget.partition

        def observed(budget: ReadBudget, count: int) -> tuple[ReadBudget, ...]:
            children = partition(budget, count)
            self.allocations.append((budget, children))
            self.grants.extend(counters(child) for child in children)
            return children

        patch = mock.patch.object(
            ReadBudget, "partition", autospec=True, side_effect=observed
        )
        patch.start()
        self.addCleanup(patch.stop)

    def cap(self, limit: int) -> None:
        """Bind an explicit synthetic cap before creating any requests."""
        self.policy = self.policy.model_copy(update={"max_input_bytes": limit})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def reconcile(self) -> tuple[str, ...]:
        """Run one batch without acquiring or committing an extra writer."""
        return self.transaction(
            lambda: review_requests.reconcile_sources(
                self.conn, self.policy, 12.0
            )
        )

    def test_reserved_shares_isolate_oversized_first_source_and_keep_gap(
        self,
    ) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        archived = tuple(
            self.conn.execute("SELECT * FROM review_source").fetchone()
        )
        self.append(
            self.path, [{"type": "noop"}] * 200 + [user_msg(300, "oversized")]
        )
        self.write(
            rollout("fresh"),
            [codex_meta("user", "fresh"), user_msg(1, "fresh")],
        )
        self.run_ingest()
        self.cap(4001)
        with mock.patch.object(
            review_originals, "_jsonl", wraps=review_originals._jsonl
        ) as reads:
            ids = self.reconcile()
        self.assertEqual(len(ids), 1)
        self.assertEqual(
            self.grants, [(2001, 2048, 50_000), (2000, 2048, 50_000)]
        )
        parent, children = self.allocations[0]
        self.assertEqual(counters(parent), (0, 0, 0))
        self.assertEqual(len(self.allocations), 1)
        self.assertGreater(self.path.stat().st_size, 2001)
        self.assertEqual(
            [call.args[0]["thread_id"] for call in reads.call_args_list],
            [TID, TID, "fresh"],
        )
        self.assertEqual(
            [call.kwargs["budget"] for call in reads.call_args_list],
            [children[0], children[0], children[1]],
        )
        self.assertEqual(children[0].bytes_left, 0)
        self.assertGreater(children[1].bytes_left, 0)
        for spent in (parent, children[0]):
            with self.assertRaisesRegex(ValueError, "evidence_limit"):
                spent.consume_bytes(1)
            with self.assertRaises(ValueError):
                spent.partition(1)
        log = self.home / "calls.jsonl"
        before_log = log.read_bytes()
        with mock.patch.dict(os.environ, {"MUNINN_NO_CALLLOG": "1"}):
            self.assertEqual(self.reconcile(), ())
        self.assertEqual(log.read_bytes(), before_log)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT * FROM review_source WHERE thread_id=?", (TID,)
                ).fetchone()
            ),
            archived,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT eligible_line,completed_line FROM review_source "
                "WHERE thread_id='fresh'"
            ).fetchone()[:],
            (2, 0),
        )

    def test_later_window_refusal_rolls_back_without_refunding_share(
        self,
    ) -> None:
        self.cap(len(self.path.read_bytes()))
        self.conn.execute("BEGIN IMMEDIATE")
        with mock.patch.object(
            review_originals, "_jsonl", wraps=review_originals._jsonl
        ) as reads:
            self.assertEqual(
                review_requests.reconcile_sources(
                    self.conn, self.policy, 12.0
                ),
                (),
            )
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_source").fetchone()[
                0
            ],
            0,
        )
        self.conn.execute("ROLLBACK")
        share = self.allocations[0][1][0]
        self.assertEqual(share.bytes_left, 0)
        self.assertEqual(share.records_left, 4094)
        self.assertEqual(
            [call.kwargs["budget"] for call in reads.call_args_list],
            [share, share],
        )

    def test_empty_sources_and_impossible_partitions_read_nothing(
        self,
    ) -> None:
        self.conn.execute("UPDATE source SET status='missing'")
        with mock.patch.object(
            review_evidence, "originals", side_effect=AssertionError("read")
        ):
            self.assertEqual(self.reconcile(), ())
        self.assertEqual(self.allocations, [])
        self.conn.execute("UPDATE source SET status='active'")
        source = dict(self.need_source())
        del source["id"]
        for count, cap in ((1, 1), (4095, 100_000)):
            for number in range(count):
                source["thread_id"] = f"{cap}-{number}"
                source["path"] = f"{cap}-{number}.jsonl"
                self.conn.execute(
                    f"INSERT INTO source({','.join(source)}) "
                    f"VALUES({','.join('?' for _ in source)})",
                    tuple(source.values()),
                )
            self.cap(cap)
            with mock.patch.object(
                review_originals, "_jsonl", side_effect=AssertionError("read")
            ):
                self.assertEqual(self.reconcile(), ())
            self.assertEqual(self.allocations, [])
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM review_source"
                ).fetchone()[0],
                0,
            )
            self.assertIn(
                '"error_code":"unavailable"',
                (self.home / "calls.jsonl").read_text(),
            )
