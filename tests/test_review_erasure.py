"""Prove governed erasure with disposable stores and originals."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from unittest import mock

from muninn import (
    erase,
    ingest,
    knowledge_erase,
    knowledge_write,
    review_requests,
    store,
    tombstone_key,
    tombstones,
)
from muninn.knowledge_models import UnresolvedProposal
from tests.ingest_support import TID
from tests.test_review_commit import CommitCase

MARKER = "ERASEGOVERNEDPRIVATECANARY"


class ReviewErasureTests(CommitCase):
    """Catch missing support closure and residual governed material."""

    def assert_row(
        self,
        sql: str,
        expected: tuple[object, ...],
        params: tuple[object, ...] = (),
    ) -> None:
        """Compare the complete selected row without hiding SQL assertions."""
        self.assertEqual(tuple(self.row(sql, params)), expected)

    def guide(self) -> dict[str, object]:
        """Build a guide with independent and dependent supported steps."""
        first = {"text": MARKER, "evidence_refs": [self.evidence[0].id]}
        second = {
            "text": "Keep testing",
            "evidence_refs": [self.evidence[1].id],
        }
        return {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": MARKER,
            "summary": first,
            "applicability": [],
            "decision_points": [],
            "pitfalls": [],
            "reason": MARKER,
            "steps": [
                {"key": "one", "claim": first, "depends_on": []},
                {"key": "two", "claim": second, "depends_on": ["one"]},
            ],
        }

    def test_partial_guide_erasure_scrubs_reason_and_dependencies(
        self,
    ) -> None:
        saved = self.save(self.result([self.guide()])).applied[0]
        view = knowledge_write.retract_knowledge(
            self.conn, saved.knowledge_id, 1, MARKER, 14.0
        )
        self.assertIsNotNone(view.body)
        summary = self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (self.evidence[0].id,), 15.0
            )
        )
        self.assertEqual((summary.evidence, summary.versions), (1, 1))
        self.assert_row(
            "SELECT state,retracted_reason FROM knowledge", ("erased", None)
        )
        self.assert_row(
            "SELECT content_json,reason,erased_at FROM knowledge_version",
            (None, None, 15.0),
        )
        claims = self.conn.execute(
            "SELECT text,dependency_keys_json,state FROM knowledge_claim"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in claims], [(None, "[]", "erased")] * 3
        )
        self.assert_row("SELECT count(*) FROM knowledge_fts", (0,))
        self.assert_row(
            "SELECT excerpt,state FROM review_evidence WHERE id=?",
            (None, "erased"),
            (self.evidence[0].id,),
        )
        self.assert_row(
            "SELECT state FROM review_evidence WHERE id=?",
            ("live",),
            (self.evidence[1].id,),
        )

    def test_erasure_reaches_prior_versions_unresolved_and_no_change(
        self,
    ) -> None:
        saved = self.save(
            self.result([self.note(content=MARKER, reason=MARKER)])
        ).applied[0]
        self.context = self.start(2, 2)
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=saved.knowledge_id,
                        expected_revision=1,
                        content=MARKER,
                        reason=MARKER,
                    )
                ]
            )
        )
        proposal = UnresolvedProposal.model_validate(
            {
                "claim": MARKER,
                "question": MARKER,
                "affected": [saved.model_dump()],
                "evidence_refs": [],
            }
        )
        self.transaction(
            lambda: knowledge_write.save_unresolved(
                self.conn, self.context.request_id, [proposal], 14.0
            )
        )
        self.context = self.start(3, 3)
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)",
            (self.context.request_id, self.evidence[0].id),
        )
        ids = tuple(e.id for e in self.evidence)
        self.context = self.context.model_copy(update={"evidence_ids": ids})
        no_change = self.result([]).model_copy(
            update={"no_change_reason": MARKER}
        )
        receipt = self.save(no_change)
        summary = self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (self.evidence[0].id,), 15.0
            )
        )
        self.context = self.context.model_copy(update={"now": 20.0})
        with mock.patch(
            "muninn.review_originals.open_regular", side_effect=AssertionError
        ):
            retried = self.save(self.result([self.note()]))
        self.assertEqual(
            retried, receipt.model_copy(update={"no_change_reason": None})
        )
        self.assertEqual(
            (summary.versions, summary.requests, summary.unresolved), (2, 3, 1)
        )
        self.assert_row(
            "SELECT count(*) FROM knowledge_version WHERE content_json "
            "IS NOT NULL OR reason IS NOT NULL",
            (0,),
        )
        self.assert_row(
            "SELECT item_json,state FROM unresolved", (None, "erased")
        )
        self.assert_row(
            "SELECT count(*) FROM review_receipt WHERE "
            "no_change_reason IS NOT NULL",
            (0,),
        )
        self.assert_row(
            "SELECT count(*) FROM review_request WHERE "
            "state!='cancelled' OR current_attempt_id IS NOT NULL",
            (0,),
        )

    def test_public_erase_removes_db_fts_wal_and_working_files(self) -> None:
        saved = self.save(self.result([self.guide()])).applied[0]
        work = self.home / "review-work" / self.context.attempt_id
        work.mkdir(parents=True)
        (work / "result.json").write_text(MARKER)
        before = self.path.read_bytes()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "UPDATE knowledge_version SET reason=?", (MARKER * 50,)
        )
        self.assert_row("PRAGMA secure_delete", (1,))
        out = erase.erase(
            self.conn, home=self.home, event_ref=f"codex:{TID}:2.1"
        )
        self.assertEqual(out["residue"], 0)
        review = out["review"]
        assert isinstance(review, dict)
        self.assertEqual(review["working_files"], 1)
        self.assertFalse(work.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_row(
            "SELECT state FROM knowledge WHERE id=?",
            ("erased",),
            (saved.knowledge_id,),
        )
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(MARKER.encode(), path.read_bytes(), path)

    def test_erase_during_review_retry_and_rebuild_cannot_resurrect(
        self,
    ) -> None:
        before = self.path.read_bytes()
        out = erase.erase(
            self.conn, home=self.home, event_ref=f"codex:{TID}:2.1"
        )
        review = out["review"]
        assert isinstance(review, dict)
        self.assertEqual(review["requests"], 1)
        with self.assertRaisesRegex(ValueError, "rejected_fenced"):
            self.save(self.result([self.note()]))
        with self.assertRaises(ValueError):
            self.transaction(
                lambda: review_requests.retry_request(
                    self.conn, self.context.request_id, 20.0
                )
            )
        key = (self.home / tombstone_key.KEY_FILE).read_bytes()
        with closing(
            store.connect_rw(self.home / "rebuilt.sqlite")
        ) as rebuilt:
            tombstones.reapply_tombstones(rebuilt, self.home)
            ingest.ingest(rebuilt, self.roots, full=True)
            self.assertEqual(
                rebuilt.execute(
                    "SELECT count(*) FROM event WHERE line=2"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            (self.home / tombstone_key.KEY_FILE).read_bytes(), key
        )
        self.assertEqual(self.path.read_bytes(), before)

    def test_retained_archive_remains_residue(self) -> None:
        self.save(self.result([self.note(content=MARKER)]))
        archive = self.home / f"{store.PRE_UPGRADE_PREFIX}retained"
        archive.write_bytes(MARKER.encode())
        out = erase.erase(self.conn, home=self.home, match=MARKER)
        self.assertEqual(out["residue"], [archive.name])
        review = out["review"]
        assert isinstance(review, dict)
        self.assertEqual(review["retained_archives"], 1)
        self.assertEqual(archive.read_bytes(), MARKER.encode())

    def test_working_files_removed_without_following_symlinks(self) -> None:
        work = self.home / "review-work" / self.context.attempt_id
        (work / "nested").mkdir(parents=True)
        (work / "prompt").write_text(MARKER)
        (work / "nested" / "result").write_text(MARKER)
        outside = self.tmp / "provider-original"
        outside.write_text(MARKER)
        (work / "original-link").symlink_to(outside)
        sibling = work.parent / ("A_" + "b" * 32)
        sibling.mkdir()
        (sibling / "prompt").write_text("Unrelated")
        self.assertEqual(
            knowledge_erase.erase_attempt_material(
                self.home, self.context.attempt_id
            ),
            3,
        )
        self.assertEqual(
            knowledge_erase.erase_attempt_material(
                self.home, self.context.attempt_id
            ),
            0,
        )
        self.assertEqual(outside.read_text(), MARKER)
        self.assertEqual((sibling / "prompt").read_text(), "Unrelated")
        with self.assertRaises(ValueError):
            knowledge_erase.erase_attempt_material(
                self.home, "../../provider-original"
            )
        with self.assertRaises(OSError):
            knowledge_erase.erase_attempt_material(self.home, sibling.name)
        work.symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaises(OSError):
            knowledge_erase.erase_attempt_material(
                self.home, self.context.attempt_id
            )
        self.assertTrue(outside.exists())

    def test_expiry_support_and_malformed_condition_are_governed(self) -> None:
        self.save(self.result([self.note()]))
        self.conn.execute(
            "DELETE FROM review_input WHERE evidence_id=?",
            (self.evidence[1].id,),
        )
        expiry = json.dumps(
            {"condition": MARKER, "evidence_refs": [self.evidence[1].id]}
        )
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=?", (expiry,)
        )
        summary = self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (self.evidence[1].id,), 15.0
            )
        )
        self.assertEqual(summary.versions, 1)
        self.assert_row(
            "SELECT content_json,expiry_json FROM knowledge_version",
            (None, None),
        )

    def prepare_wal_replay(self) -> None:
        """Leave committed governed content and a durable erase intent."""
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=0")
        self.save(self.result([self.note(content=MARKER, reason=MARKER)]))
        record = self.evidence[0]
        tombstones.append_tombstones(
            self.home,
            [
                tombstones.tombstone_row(
                    record.provider,
                    "line",
                    thread_id=record.thread_id,
                    line=record.line,
                    line_sha256=record.line_sha256,
                )
            ],
        )

    def test_write_ahead_replay_scrubs_preexisting_derivations(self) -> None:
        self.prepare_wal_replay()
        self.assertEqual(
            tombstones.reapply_tombstones(self.conn, self.home), 1
        )
        self.assertEqual(erase.residue_scan(self.home, [MARKER.encode()]), [])
        self.assert_row("SELECT state FROM knowledge", ("erased",))
        self.assert_row(
            "SELECT excerpt FROM review_evidence WHERE id=?",
            (None,),
            (self.evidence[0].id,),
        )

    def test_busy_replay_preserves_intent_and_retry_cleans_wal(self) -> None:
        self.prepare_wal_replay()
        intent = (self.home / tombstones.TOMBSTONE_FILE).read_bytes()
        original = self.path.read_bytes()
        with closing(sqlite3.connect(store.db_path(self.home))) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM knowledge_version").fetchall()
            with self.assertRaisesRegex(OSError, "^erasure_checkpoint_busy$"):
                tombstones.reapply_tombstones(self.conn, self.home)
            self.assertFalse(self.conn.in_transaction)
            self.assert_row("SELECT state FROM knowledge", ("erased",))
            self.assert_row("SELECT count(*) FROM tombstone", (1,))
            self.assertIn(
                "muninn.sqlite-wal",
                erase.residue_scan(self.home, [MARKER.encode()]),
            )
        self.assertEqual(
            tombstones.reapply_tombstones(self.conn, self.home), 0
        )
        self.assertEqual(erase.residue_scan(self.home, [MARKER.encode()]), [])
        self.assertEqual(
            (self.home / tombstones.TOMBSTONE_FILE).read_bytes(), intent
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_distinct_valid_expiry_support_is_erased(self) -> None:
        self.save(self.result([self.note()]))
        expiry = json.dumps(
            {
                "at": None,
                "condition": MARKER,
                "evidence_refs": [self.evidence[1].id],
            }
        )
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=?", (expiry,)
        )
        out = erase.erase(self.conn, home=self.home, match=MARKER)
        self.assertEqual(out["residue"], 0)
        self.assert_row(
            "SELECT content_json,expiry_json FROM knowledge_version",
            (None, None),
        )

    def test_support_erasure_obeys_caller_transaction(self) -> None:
        self.save(self.result([self.note(content=MARKER)]))
        with self.assertRaisesRegex(ValueError, "caller_transaction_required"):
            knowledge_erase.erase_review_support(
                self.conn, (self.evidence[0].id,), 15.0
            )
        self.conn.execute("BEGIN IMMEDIATE")
        knowledge_erase.erase_review_support(
            self.conn, (self.evidence[0].id,), 15.0
        )
        self.conn.rollback()
        self.assert_row("SELECT state FROM knowledge", ("active",))

    def test_match_decodes_governed_json_text(self) -> None:
        title = 'Quoted "private" title'
        guide = self.guide() | {"title": title}
        self.save(self.result([guide]))
        erase.erase(self.conn, home=self.home, match=title)
        self.assert_row("SELECT state FROM knowledge", ("erased",))

    def test_review_consumers_import_without_a_cycle(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import muninn.knowledge_write"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
