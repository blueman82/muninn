"""Prove atomic commit shares one original-read allowance across phases."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from muninn import (
    cursor_import,
    review_evidence,
    review_originals,
    review_requests,
)
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    EvidenceWindow,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import line
from tests.test_classify import user_msg
from tests.test_review_commit import CommitCase


class ReviewCommitBudgetTests(CommitCase):
    """Keep physical readers real while observing their operation budget."""

    def claim_inputs(self, window: EvidenceWindow, limit: int) -> None:
        """Attach selected reads after claim, as later review reads would."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        self.policy = self.policy.model_copy(
            update={
                "max_input_bytes": limit,
                "policy_id": self.context.request_id,
            }
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        request = self.enqueue(window)
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 15.0
            )
        )
        self.conn.executemany(
            "INSERT INTO review_input VALUES(?,?)",
            [(request.id, e.id) for e in self.evidence],
        )
        self.context = CommitContext(
            home=self.home,
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            evidence_ids=tuple(e.id for e in self.evidence),
            now=16.0,
        )

    def test_receipt_retry_reads_no_originals(self) -> None:
        receipt = self.save(self.result([self.note()]))
        self.path.unlink()
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
            self.assertEqual(self.save(self.result([])), receipt)

    def test_final_acceptance_reuses_one_fresh_shared_original(self) -> None:
        self.records[1] = user_msg(1, "Use Python. Please test.")
        self.records[1]["metadata"] = "x" * 16_000
        self.path.write_bytes(b"".join(line(r) for r in self.records))
        self.run_ingest(full=True)
        window = self.window(2, 2)
        self.evidence = tuple(
            review_evidence.select_evidence(
                self.home,
                EvidenceLocator(
                    source=window.source,
                    line=2,
                    part=1,
                    line_sha256=window.boundary_hash,
                ),
                EvidenceSelector(
                    json_pointer="/payload/content/0/text",
                    start=start,
                    end=end,
                ),
            )
            for start, end in ((0, 11), (12, 24))
        )
        for record in self.evidence:
            self.persist(record)
        exact = len(self.path.read_bytes()) + len(line(self.records[1]))
        self.claim_inputs(window, exact)
        receipt = self.save(self.result([self.note()]))
        self.assertEqual(receipt.outcome, "completed")
        self.assertEqual(len(receipt.applied), 1)

    def test_unselected_boundary_change_rejects_no_change(self) -> None:
        self.evidence = self.evidence[:1]
        self.claim_inputs(self.window(), 4096)
        self.path.write_bytes(
            self.path.read_bytes().replace(b"a one", b"b one")
        )
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_one_budget_covers_all_actions_and_dependency_support(
        self,
    ) -> None:
        claim = {"text": "Use Python.", "evidence_refs": [self.evidence[0].id]}
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Python",
            "summary": claim,
            "applicability": [],
            "decision_points": [],
            "pitfalls": [],
            "reason": "Owner instruction",
            "steps": [
                {"key": "one", "claim": claim, "depends_on": []},
                {
                    "key": "two",
                    "claim": {
                        "text": "Observed reply",
                        "evidence_refs": [self.evidence[1].id],
                    },
                    "depends_on": ["one"],
                },
            ],
        }
        source = self.path.read_bytes()
        policy = self.row(
            "SELECT policy_json,policy_digest FROM review_request"
        )[0:]
        with (
            mock.patch.object(
                review_originals, "_jsonl", wraps=review_originals._jsonl
            ) as prefix,
            mock.patch.object(
                review_evidence, "reread", wraps=review_evidence.reread
            ) as selected,
        ):
            receipt = self.save(self.result([self.note(), guide]))
        self.assertEqual(len(receipt.applied), 2)
        self.assertEqual((prefix.call_count, selected.call_count), (1, 2))
        budget = prefix.call_args.kwargs["budget"]
        self.assertTrue(
            all(c.kwargs["budget"] is budget for c in selected.call_args_list)
        )
        self.assertEqual(
            budget.bytes_left,
            4096 - len(source) - sum(len(line(r)) for r in self.records[1:]),
        )
        self.assertEqual(budget.records_left, 4091)
        self.assertEqual(self.path.read_bytes(), source)
        self.assertEqual(
            self.row("SELECT policy_json,policy_digest FROM review_request")[
                :
            ],
            policy,
        )

    def test_combined_bytes_refuse_then_exact_fit_succeeds(self) -> None:
        exact = len(self.path.read_bytes()) + sum(
            len(line(r)) for r in self.records[1:]
        )
        for limit in (exact - 1, exact):
            with self.subTest(limit=limit):
                self.claim_inputs(self.window(), limit)
                review_evidence.validate_window_boundary(
                    self.conn, self.window(), limit
                )
                review_evidence.validate_evidence(
                    self.conn,
                    self.home,
                    self.context.evidence_ids,
                    self.scope,
                    self.policy,
                )
                before = self.counts()
                with (
                    mock.patch.object(
                        review_originals,
                        "_jsonl",
                        wraps=review_originals._jsonl,
                    ) as prefix,
                    mock.patch.object(
                        review_evidence, "reread", wraps=review_evidence.reread
                    ) as selected,
                ):
                    if limit == exact:
                        self.assertEqual(
                            self.save(self.result([self.note()])).outcome,
                            "completed",
                        )
                    else:
                        with self.assertRaisesRegex(
                            ValueError, "evidence_byte_limit"
                        ):
                            self.save(self.result([self.note()]))
                        self.assertEqual(self.counts(), before)
                self.assertEqual(prefix.call_count, 1)
                budget = prefix.call_args.kwargs["budget"]
                self.assertIs(selected.call_args.kwargs["budget"], budget)
                self.assertLess(budget.bytes_left, limit)
                if limit == exact:
                    self.assertEqual(budget.bytes_left, 0)

    def test_combined_records_refuse_then_exact_fit_succeeds(self) -> None:
        self.evidence = self.evidence[:1]
        originals = self.records[:2]
        for count in (4096, 4095):
            self.records = originals + [{"type": "noop"}] * (count - 2)
            self.path.write_bytes(b"".join(line(r) for r in self.records))
            self.run_ingest(full=True)
            self.claim_inputs(self.window(2, count), 100_000)
            before = self.counts()
            with (
                mock.patch.object(
                    review_originals, "_jsonl", wraps=review_originals._jsonl
                ) as prefix,
                mock.patch.object(
                    review_evidence, "reread", wraps=review_evidence.reread
                ) as selected,
            ):
                if count == 4095:
                    self.assertEqual(
                        self.save(self.result([self.note()])).outcome,
                        "completed",
                    )
                else:
                    with self.assertRaisesRegex(
                        ValueError, "evidence_record_limit"
                    ):
                        self.save(self.result([self.note()]))
                    self.assertEqual(self.counts(), before)
            budget = prefix.call_args.kwargs["budget"]
            self.assertIs(selected.call_args.kwargs["budget"], budget)
            self.assertEqual(budget.records_left, 0)

    def native(self) -> tuple[Path, EvidenceWindow]:
        """Build an unindexed native source and select its one visible cell."""
        path = self.tmp / "commit.sqlite"
        raw = b'{"type":1,"text":"Please use Python."}'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT,value BLOB)")
            db.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                [
                    (
                        "composerData:chat",
                        b'{"fullConversationHeadersOnly":[{"bubbleId":"one"}]}',
                    ),
                    ("bubbleId:chat:one", raw),
                ],
            )
        cursor_import.run(self.home, path, str(self.tmp), 0.0)
        window = EvidenceWindow(
            source=SourceRef(provider="cursor", thread_id="chat"),
            from_line=1,
            through_line=1,
            boundary_hash=hashlib.sha256(raw).hexdigest(),
        )
        record = review_evidence.select_evidence(
            self.home,
            EvidenceLocator(
                source=window.source,
                line=1,
                part=1,
                line_sha256=window.boundary_hash,
            ),
            EvidenceSelector(json_pointer="/text", start=None, end=None),
        )
        self.persist(record)
        self.evidence = (record,)
        self.scope = record.scope
        self.claim_inputs(window, 100_000)
        return path, window

    def test_native_snapshots_share_work_and_close_on_refusal(self) -> None:
        path, window = self.native()
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany(
                "INSERT INTO cursorDiskKV VALUES(?,?)",
                ((f"unrelated:{i}", b"{}") for i in range(7200)),
            )
        seen: list[tuple[ReadBudget, sqlite3.Connection]] = []
        connect = review_originals._cursor_connection

        def observe(
            stack: ExitStack, database: Path, *, budget: ReadBudget
        ) -> sqlite3.Connection:
            conn = connect(stack, database, budget=budget)
            seen.append((budget, conn))
            return conn

        source = path.read_bytes()
        for phase in ("boundary", "selected", "commit"):
            with mock.patch.object(
                review_originals, "_cursor_connection", side_effect=observe
            ):
                if phase == "boundary":
                    review_evidence.validate_window_boundary(
                        self.conn, window, 100_000
                    )
                elif phase == "selected":
                    review_evidence.validate_evidence(
                        self.conn,
                        self.home,
                        self.context.evidence_ids,
                        self.scope,
                        self.policy,
                    )
                else:
                    with self.assertRaisesRegex(
                        ValueError, "evidence_work_limit"
                    ):
                        self.save(self.result([self.note()]))
            for _, conn in seen:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
        self.assertEqual(len(seen), 4)
        self.assertIs(seen[2][0], seen[3][0])
        self.assertGreater(seen[0][0].sql_steps_left, 0)
        self.assertGreater(seen[1][0].sql_steps_left, 0)
        self.assertEqual(seen[2][0].sql_steps_left, 0)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(path.read_bytes(), source)

    def test_native_success_closes_snapshots(self) -> None:
        path, _ = self.native()
        source = path.read_bytes()
        self.assertEqual(
            self.save(self.result([self.note()])).outcome, "completed"
        )
        self.assertEqual(path.read_bytes(), source)
        with closing(sqlite3.connect(path, timeout=0)) as db:
            db.execute("BEGIN EXCLUSIVE")
            db.rollback()
