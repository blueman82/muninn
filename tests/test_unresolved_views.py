"""Inspect historical questions without admitting or changing evidence."""

from __future__ import annotations

from muninn import decision_read
from muninn.knowledge_models import Scope
from tests.decision_support import DecisionCase


class UnresolvedTests(DecisionCase):
    """Distinguish historical questions from their current relevance."""

    def question(self, affected: list[dict[str, object]] | None = None) -> str:
        """Commit one genuine question with independent retained support."""
        return self.save(
            self.result(
                [],
                [
                    {
                        "claim": "Applicability uncertain",
                        "affected": affected or [],
                        "evidence_refs": [],
                        "question": "Which condition applies?",
                    }
                ],
            )
        ).unresolved_ids[0]

    def test_empty_affected_uses_origin_scope_and_reads_do_not_write(
        self,
    ) -> None:
        ident = self.question()
        before = self.projection()
        view = decision_read.show_unresolved(self.conn, ident)
        self.assertEqual(view.scope, self.scope)
        self.assertEqual(view.affected, ())
        self.assertEqual(view.state, "open")
        self.assertEqual(
            decision_read.list_unresolved(self.conn, (self.scope,)), (view,)
        )
        self.assertEqual(
            decision_read.list_unresolved(
                self.conn, (Scope(project_id="/wrong", task=None),)
            ),
            (),
        )
        self.assertEqual(self.projection(), before)

    def test_erased_null_and_malformed_rows_have_finite_behavior(self) -> None:
        ident = self.question()
        self.conn.execute(
            "UPDATE unresolved SET item_json=NULL,state='erased'"
        )
        self.assertIsNone(decision_read.show_unresolved(self.conn, ident).item)
        self.assertEqual(
            decision_read.list_unresolved(self.conn, (self.scope,)), ()
        )
        self.conn.execute("UPDATE unresolved SET item_json='{}',state='open'")
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "^unresolved_invalid$"):
            decision_read.show_unresolved(self.conn, ident)
        self.assertEqual(before, self.projection())
        with self.assertRaisesRegex(ValueError, "^unresolved_unknown$"):
            decision_read.show_unresolved(self.conn, "U_" + "f" * 32)

    def test_replacement_marks_old_affected_stale_but_question_stays_open(
        self,
    ) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(2, 2)
        ident = self.question([saved.model_dump()])
        self.context = self.start(3, 3)
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
        before = self.projection()
        view = decision_read.show_unresolved(self.conn, ident)
        self.assertEqual(view.state, "open")
        self.assertEqual(view.affected, (saved,))
        self.assertEqual(view.stale_affected, (saved,))
        self.assertEqual(before, self.projection())

    def test_limits_and_cursor_are_deterministic(self) -> None:
        self.question()
        self.context = self.start(2, 2)
        self.question()
        views = decision_read.list_unresolved(self.conn, (self.scope,))
        self.assertEqual([v.id for v in views], sorted(v.id for v in views))
        self.assertEqual(
            decision_read.list_unresolved(
                self.conn, (self.scope,), limit=1, after=views[0].id
            ),
            (views[1],),
        )
        for limit in (0, 101, True):
            with self.assertRaisesRegex(ValueError, "^unresolved_limit$"):
                decision_read.list_unresolved(
                    self.conn, (self.scope,), limit=limit
                )
