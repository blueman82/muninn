"""Exercise atomic checked knowledge writes against disposable originals."""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

from muninn import (
    erase,
    knowledge_write,
    review_commit,
    review_evidence,
    review_requests,
    store,
)
from muninn.knowledge_models import ReviewResult
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import EvidenceRecord
from muninn.review_json import canonical_json
from muninn.review_models import Receipt
from tests.ingest_support import primary, rollout
from tests.test_classify import user_msg
from tests.test_review_requests import RequestCase


class LostResponseConnection(sqlite3.Connection):
    """Lose the response after SQLite has durably committed a transaction."""

    def commit(self) -> None:
        """Commit once and emulate a process interruption before return."""
        super().commit()
        raise InterruptedError("response lost")


class CommitCase(RequestCase):
    """Supply selected originals and a claimed disposable review request."""

    evidence: tuple[EvidenceRecord, ...]

    def setUp(self) -> None:
        super().setUp()
        self.records[1] = user_msg(1, "Use Python for this project.")
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.evidence = review_evidence.initial_evidence(
            self.home, self.window(), 4096
        ).excerpts
        for record in self.evidence:
            self.persist(record)
        self.context = self.start()

    def persist(self, record: EvidenceRecord) -> None:
        """Persist evidence selected from synthetic physical originals."""
        data = record.model_dump(mode="json")
        for key in ("scope", "selector", "original_locator"):
            data[f"{key}_json"] = json.dumps(data.pop(key))
        names, marks = ",".join(data), ",".join("?" for _ in data)
        self.conn.execute(
            f"INSERT INTO review_evidence({names}) VALUES({marks})",
            tuple(data.values()),
        )

    def start(self, first: int = 2, last: int = 3) -> CommitContext:
        """Admit and claim a request with all its actual selected input."""
        request = self.enqueue(self.window(first, last))
        ids = tuple(e.id for e in self.evidence if first <= e.line <= last)
        self.conn.executemany(
            "INSERT INTO review_input VALUES(?,?)",
            [(request.id, ident) for ident in ids],
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        return CommitContext(
            home=self.home,
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            evidence_ids=ids,
            now=13.0,
        )

    def note(self, **updates: object) -> dict[str, object]:
        """Build a supported note without program lifecycle fields."""
        return {
            "action": "add",
            "form": "note",
            "kind": "fact",
            "scope": self.scope.model_dump(),
            "content": "Use Python.",
            "conditions": [],
            "evidence_refs": [self.evidence[0].id],
            "reason": "The owner requested Python.",
            **updates,
        }

    def result(
        self,
        changes: list[dict[str, object]],
        unresolved: list[dict[str, object]] | None = None,
    ) -> ReviewResult:
        """Parse a proposal using the strict external content contract."""
        return ReviewResult.model_validate_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "changes": changes,
                    "unresolved": unresolved or [],
                    "no_change_reason": (
                        None if changes or unresolved else "No change."
                    ),
                }
            )
        )

    def save(self, result: ReviewResult) -> Receipt:
        """Call the real checked transaction against the isolated store."""
        return review_commit.commit_review(self.conn, self.context, result)

    def row(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
        """Read one independently asserted durable result row."""
        return self.conn.execute(sql, params).fetchone()

    def counts(self) -> tuple[int, ...]:
        """Observe every content table, including normalized rows and FTS."""
        return tuple(
            self.row(f"SELECT count(*) FROM {table}")[0]
            for table in (
                "knowledge",
                "knowledge_version",
                "knowledge_claim",
                "knowledge_support",
                "unresolved",
                "knowledge_fts",
                "review_receipt",
            )
        )


class ReviewCommitTests(CommitCase):
    """Catch partial batches, stale writers and unsupported authority."""

    def test_invalid_batch_writes_nothing(self) -> None:
        bad = self.note(evidence_refs=["E_" + "f" * 32])
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([self.note(), bad]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        row = self.row("SELECT state,retry_at FROM review_request")
        self.assertEqual(tuple(row), ("failed", None))
        self.assertEqual(
            self.row("SELECT error_code FROM review_attempt")[0],
            "rejected_evidence",
        )

    def test_crash_after_commit_returns_same_receipt(self) -> None:
        path = self.row("PRAGMA database_list")[2]
        with sqlite3.connect(
            path, factory=LostResponseConnection, isolation_level=None
        ) as conn:
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            with self.assertRaises(InterruptedError):
                review_commit.commit_review(
                    conn, self.context, self.result([self.note()])
                )
        before = self.row("SELECT * FROM review_receipt")
        receipt = self.save(self.result([]))
        after = self.row("SELECT * FROM review_receipt")
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(receipt.request_id, self.context.request_id)
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))

    def test_stale_revision_generation_scope_or_evidence_refused(self) -> None:
        for fault, code in (
            ("generation", "rejected_fenced"),
            ("scope", "rejected_scope"),
            ("evidence", "rejected_evidence"),
            ("revision", "rejected_revision"),
        ):
            with self.subTest(fault=fault):
                case = ReviewCommitTests()
                case.setUp()
                try:
                    change = case.note()
                    if fault == "generation":
                        case.conn.execute(
                            "UPDATE review_control SET generation=2"
                        )
                    elif fault == "scope":
                        change["scope"] = {"project_id": "wrong", "task": None}
                    elif fault == "evidence":
                        case.path.write_bytes(
                            case.path.read_bytes().replace(
                                b"Python", b"Golang"
                            )
                        )
                    else:
                        change.update(
                            action="replace",
                            knowledge_id="K_" + "a" * 32,
                            expected_revision=1,
                        )
                    with self.assertRaisesRegex(ValueError, code):
                        case.save(case.result([change]))
                    self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))
                finally:
                    case.doCleanups()

    def test_unresolved_withholds_affected_dependencies(self) -> None:
        claim = {"text": "Use Python.", "evidence_refs": [self.evidence[0].id]}
        guide: dict[str, object] = {
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
                {"key": "two", "claim": claim, "depends_on": ["one"]},
            ],
        }
        saved = self.save(self.result([guide])).applied[0]
        self.context = self.start(2, 2)
        unresolved = {
            "claim": "Applicability uncertain",
            "affected": [saved.model_dump()],
            "evidence_refs": [],
            "question": "What missing evidence establishes applicability?",
        }
        receipt = self.save(self.result([self.note()], [unresolved]))
        self.assertEqual(receipt.outcome, "completed_unresolved")
        self.assertEqual(len(receipt.unresolved_ids), 1)
        states = self.conn.execute(
            "SELECT state FROM knowledge_claim WHERE knowledge_id=?",
            (saved.knowledge_id,),
        ).fetchall()
        self.assertEqual([r[0] for r in states], ["withheld"] * 3)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_id=?",
                (saved.knowledge_id,),
            )[0],
            0,
        )
        self.assertEqual(
            self.row(
                "SELECT state FROM knowledge WHERE id=?",
                (receipt.applied[0].knowledge_id,),
            )[0],
            "active",
        )

    def test_preference_replace_requires_direct_owner_support(self) -> None:
        saved = self.save(self.result([self.note(kind="preference")])).applied[
            0
        ]
        self.context = self.start(3, 3)
        self.conn.execute(
            "UPDATE review_request SET origin='proposal' WHERE id=?",
            (self.context.request_id,),
        )
        replacement = self.note(
            action="replace",
            knowledge_id=saved.knowledge_id,
            expected_revision=1,
            kind="fact",
            evidence_refs=[self.evidence[1].id],
        )
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.save(self.result([replacement]))
        self.assertEqual(
            self.row("SELECT current_revision FROM knowledge")[0],
            1,
        )

    def test_no_change_completes_without_knowledge(self) -> None:
        self.conn.execute(
            "UPDATE review_source SET activation_line=1,completed_line=1"
        )
        receipt = self.save(self.result([]))
        self.assertEqual(receipt.outcome, "no_change")
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))
        self.assertEqual(
            self.row(
                "SELECT activation_line,completed_line FROM review_source"
            )[:],
            (1, 3),
        )
        self.assertEqual(
            self.row("SELECT state FROM review_request")[0],
            "completed",
        )
        self.assertEqual(
            self.row("SELECT ended_at,outcome FROM review_attempt")[:],
            (13.0, "no_change"),
        )

    def test_retract_requires_expected_revision(self) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        with self.assertRaisesRegex(ValueError, "rejected_revision"):
            knowledge_write.retract_knowledge(
                self.conn, saved.knowledge_id, 2, "Withdraw.", 14.0
            )
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))
        known = self.start(2, 2)
        view = knowledge_write.retract_knowledge(
            self.conn, saved.knowledge_id, 1, "Withdraw.", 14.0
        )
        self.assertEqual(
            (view.state, view.retracted_reason), ("retracted", "Withdraw.")
        )
        self.assertIsNotNone(view.body)
        self.assertEqual(
            self.row(
                "SELECT state,current_attempt_id FROM review_request "
                "WHERE id=?",
                (known.request_id,),
            )[:],
            ("cancelled", None),
        )
        self.context = self.start(3, 3)
        with self.assertRaisesRegex(ValueError, "rejected_revision"):
            self.save(
                self.result(
                    [
                        self.note(
                            action="replace",
                            knowledge_id=saved.knowledge_id,
                            expected_revision=1,
                            evidence_refs=[self.evidence[1].id],
                        )
                    ]
                )
            )
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_fts")[0],
            0,
        )

    def test_write_failures_rollback_all_content(self) -> None:
        for table in (
            "knowledge_version",
            "knowledge_support",
            "unresolved",
            "review_receipt",
            "review_source",
        ):
            with self.subTest(table=table):
                operation = "UPDATE" if table == "review_source" else "INSERT"
                self.conn.execute(
                    f"CREATE TEMP TRIGGER fail BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'injected'); END"
                )
                unresolved = {
                    "claim": "Unknown",
                    "affected": [],
                    "evidence_refs": [],
                    "question": "What missing evidence supports this?",
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    self.save(self.result([self.note()], [unresolved]))
                self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
                self.assertFalse(self.conn.in_transaction)
                self.conn.execute("DROP TRIGGER fail")

    def test_cancelled_attempt_and_erased_ancestor_never_save(self) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.save(self.result([self.note()]))
        self.context = self.start(2, 2)
        self.write(rollout("missing"), primary("missing")[:2])
        self.run_ingest()
        erase.erase(self.conn, home=self.home, event_ref="codex:missing:2.1")
        self.assertTrue((self.home / "tombstone.key").is_file())
        self.assertEqual(self.row("SELECT count(*) FROM tombstone")[0], 2)
        self.conn.execute("DELETE FROM source WHERE thread_id='missing'")
        self.conn.execute("UPDATE source SET forked_from_id='missing'")
        with self.assertRaisesRegex(ValueError, "rejected_erased"):
            self.save(self.result([self.note()]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_all_durable_input_checked_even_if_uncited(self) -> None:
        self.conn.execute(
            "UPDATE review_evidence SET state='erased' WHERE id=?",
            (self.evidence[1].id,),
        )
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([self.note()]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_nested_transaction_is_preserved(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "transaction"):
            self.save(self.result([self.note()]))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_policy_change_rejects_without_mutating_snapshot(self) -> None:
        original = canonical_json(self.policy)
        self.conn.execute(
            "UPDATE review_control SET policy_digest=?", ("f" * 64,)
        )
        with self.assertRaisesRegex(ValueError, "rejected_validation"):
            self.save(self.result([self.note()]))
        self.assertEqual(
            self.row("SELECT policy_json FROM review_request")[0],
            original,
        )

    def test_commit_does_not_reacquire_writer(self) -> None:
        with (
            store.writer_lock(self.home),
            mock.patch(
                "muninn.store.writer_lock",
                side_effect=AssertionError("nested lock"),
            ),
        ):
            receipt = self.save(self.result([self.note()]))
        self.assertEqual(receipt.outcome, "completed")
