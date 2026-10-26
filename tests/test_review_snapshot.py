"""Reject results that used an older knowledge view."""

from __future__ import annotations

from muninn import review_snapshot
from tests.test_review_commit import CommitCase


class KnowledgeSnapshotTests(CommitCase):
    """Exercise snapshot checks inside the actual commit transaction."""

    def test_concurrent_new_knowledge_rejects_old_result(self) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(3, 3)
        self.context = self.context.model_copy(
            update={
                "knowledge_digest": review_snapshot.snapshot(
                    self.conn, self.scope
                )
            }
        )
        self.conn.execute(
            "UPDATE knowledge SET state='retracted' WHERE id=?",
            (saved.knowledge_id,),
        )
        with self.assertRaisesRegex(ValueError, "rejected_revision"):
            self.save(self.result([]))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_receipt"
            ).fetchone()[0],
            1,
        )

    def test_unchanged_knowledge_can_commit(self) -> None:
        self.context = self.context.model_copy(
            update={
                "knowledge_digest": review_snapshot.snapshot(
                    self.conn, self.scope
                )
            }
        )
        self.assertEqual(self.save(self.result([])).outcome, "no_change")
