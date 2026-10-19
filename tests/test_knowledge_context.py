"""Check authoritative current retrieval against disposable governed state."""

from __future__ import annotations

import time
from unittest import mock

from muninn import knowledge_push, knowledge_read, knowledge_write
from muninn.knowledge_models import (
    AffectedRevision,
    Scope,
    UnresolvedProposal,
)
from muninn.review_json import canonical_json
from tests.knowledge_context_support import KnowledgeContextCase


class KnowledgeContextTests(KnowledgeContextCase):
    """Exercise public retrieval with real normalized claims and support."""

    def test_only_current_applicable_supported_guidance(self) -> None:
        wanted = self.add()
        unsupported = self.add(content="Unsupported Python.")
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=?",
            (unsupported,),
        )
        other = Scope(project_id="other-project", task=None)
        foreign = self.add(scope=other.model_dump())
        actual = self.context_view()
        self.assertEqual([view.id for view in actual.notes], [wanted])
        self.assertEqual(
            [view.id for view in self.search()],
            [wanted],
        )
        self.assertNotIn(foreign, [view.id for view in actual.notes])

    def test_context_is_notes_and_guide_index_with_revisions(self) -> None:
        note, guide = self.add(), self.guide()
        context = self.context_view()
        self.assertEqual(
            [(item.id, item.revision) for item in context.notes], [(note, 1)]
        )
        self.assertEqual(
            [
                (item.id, item.revision, item.title, item.scope)
                for item in context.guides
            ],
            [(guide, 1, "Python guide", self.scope)],
        )
        self.assertFalse(context.truncated)
        self.assertEqual(context.unresolved_ids, [])
        self.assertNotIn("steps", canonical_json(context))

    def test_conflict_expiry_erasure_excluded(self) -> None:
        wanted = self.add()
        for state in ("withheld", "retracted", "erased"):
            ident = self.add()
            self.conn.execute(
                "UPDATE knowledge SET state=? WHERE id=?", (state, ident)
            )
        expired = self.add()
        self.set_expiry(expired, ref=0, at=1.0)
        disputed = self.add()
        proposal = UnresolvedProposal(
            claim="Conflict",
            affected=[AffectedRevision(knowledge_id=disputed, revision=1)],
            evidence_refs=[self.evidence[0].id],
            question="Which applies?",
        )
        unresolved = knowledge_write.save_unresolved(
            self.conn, self.context.request_id, [proposal], 14.0
        )
        self.conn.execute(
            "UPDATE knowledge SET state='active' WHERE id=?", (disputed,)
        )
        self.assertEqual(
            [view.id for view in self.context_view().notes], [wanted]
        )
        self.assertEqual(self.context_view().unresolved_ids, unresolved)
        self.assertEqual(
            [view.id for view in self.search()],
            [wanted],
        )
        self.assertGreater(
            self.row("SELECT count(*) FROM knowledge_fts")[0], 1
        )

    def test_pull_refresh_observes_replacement(self) -> None:
        ident = self.add(content="Original Python guidance.")
        self.add(
            action="replace",
            knowledge_id=ident,
            expected_revision=1,
            content="Replacement Rust guidance.",
        )
        self.conn.execute(
            "INSERT INTO knowledge_fts VALUES(?,1,'Original Python guidance')",
            (ident,),
        )
        self.assertEqual(
            self.search(),
            (),
        )
        latest = self.search("Rust")
        self.assertEqual(
            [(view.id, view.revision) for view in latest], [(ident, 2)]
        )
        self.assertEqual(
            knowledge_read.show_knowledge(self.conn, ident, None).revision, 2
        )
        self.assertEqual(
            knowledge_read.show_knowledge(self.conn, ident, 1).revision, 1
        )
        self.assertEqual(self.context_view().notes[0].revision, 2)

    def test_stored_support_mismatch_never_grants_admission(self) -> None:
        ident = self.add()
        expected = (self.evidence[0].id,)
        self.assertEqual(
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            ),
            expected,
        )
        self.conn.execute(
            "UPDATE knowledge_support SET evidence_id=? WHERE knowledge_id=?",
            (self.evidence[1].id, ident),
        )
        self.assertEqual(self.context_view().notes, [])
        self.assertEqual(
            self.search(),
            (),
        )
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            )

    def test_support_state_source_projection_and_tombstones(self) -> None:
        ident = self.add()
        for column, value in (
            ("state", "changed"),
            ("projection_version", "old"),
            ("source_version", "old"),
            (
                "scope_json",
                canonical_json(Scope(project_id="other", task=None)),
            ),
        ):
            with self.subTest(column=column):
                original = self.row(
                    f"SELECT {column} FROM review_evidence WHERE id=?",
                    (self.evidence[0].id,),
                )[0]
                self.conn.execute(
                    f"UPDATE review_evidence SET {column}=? WHERE id=?",
                    (value, self.evidence[0].id),
                )
                self.assertEqual(self.context_view().notes, [])
                self.conn.execute(
                    f"UPDATE review_evidence SET {column}=? WHERE id=?",
                    (original, self.evidence[0].id),
                )
        self.conn.execute("UPDATE source SET status='missing'")
        self.assertEqual(self.context_view().notes, [])
        self.conn.execute("UPDATE source SET status='active'")
        evidence = self.evidence[0]
        self.conn.execute(
            "INSERT INTO tombstone(created_at,provider,level,thread_id,line,"
            "line_sha256) VALUES(20.0,?,'line',?,?,?)",
            (
                evidence.provider,
                evidence.thread_id,
                evidence.line,
                evidence.line_sha256,
            ),
        )
        self.assertEqual(
            self.search(),
            (),
        )
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            )

    def test_mixed_guide_dependency_is_withheld(self) -> None:
        ident = self.guide()
        self.conn.execute(
            "UPDATE knowledge_claim SET state='withheld' WHERE knowledge_id=? "
            "AND claim_key='step/first'",
            (ident,),
        )
        self.assertEqual(self.context_view().guides, [])
        self.assertEqual(
            self.search(),
            (),
        )

    def test_support_requires_exact_current_active_scope_and_time(
        self,
    ) -> None:
        ident = self.add()
        for revision, scope in (
            (2, self.scope),
            (1, Scope(project_id="unknown", task=None)),
        ):
            with (
                self.subTest(revision=revision, scope=scope),
                self.assertRaises(ValueError),
            ):
                knowledge_read.knowledge_support(
                    self.conn, ident, revision, scope, 20.0
                )
        self.conn.execute(
            "UPDATE knowledge SET state='withheld' WHERE id=?", (ident,)
        )
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            )
        self.assertEqual(
            knowledge_read.show_knowledge(self.conn, ident, None).state,
            "withheld",
        )

    def test_show_retains_retraction_and_clears_erased_history(self) -> None:
        ident = self.add()
        knowledge_write.retract_knowledge(
            self.conn, ident, 1, "Owner withdrew it.", 14.0
        )
        view = knowledge_read.show_knowledge(self.conn, ident, None)
        self.assertEqual(
            (view.state, view.retracted_reason),
            ("retracted", "Owner withdrew it."),
        )
        self.assertIsNotNone(view.body)
        self.conn.execute(
            "UPDATE knowledge SET state='erased',retracted_reason=NULL "
            "WHERE id=?",
            (ident,),
        )
        self.assertIsNone(
            knowledge_read.show_knowledge(self.conn, ident, 1).body
        )
        with self.assertRaisesRegex(ValueError, "knowledge_not_found"):
            knowledge_read.show_knowledge(self.conn, "K_" + "f" * 32, None)

    def test_context_utf8_envelope_budget_and_read_only(self) -> None:
        self.add(content="Python café 東京.")
        complete = self.context_view()
        size = len(canonical_json(complete).encode())
        self.assertEqual(self.context_view(size), complete)
        partial = self.context_view(size - 1)
        self.assertTrue(partial.truncated)
        self.assertEqual(partial.notes, [])
        self.assertLessEqual(len(canonical_json(partial).encode()), size - 1)
        before = self.conn.total_changes
        self.context_view()
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaisesRegex(ValueError, "context_limit"):
            self.context_view(1)

    def test_expiry_support_and_owner_authority_are_required(self) -> None:
        ident = (
            self.save(self.result([self.note(kind="preference")]))
            .applied[0]
            .knowledge_id
        )
        self.conn.execute(
            "UPDATE review_evidence SET authority='reported' WHERE id=?",
            (self.evidence[0].id,),
        )
        self.assertEqual(self.context_view().notes, [])
        self.conn.execute(
            "UPDATE review_evidence SET authority='direct_owner' WHERE id=?",
            (self.evidence[0].id,),
        )
        self.set_expiry(ident, at=time.time() + 1000)
        self.assertEqual(
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            ),
            tuple(sorted(record.id for record in self.evidence)),
        )
        self.conn.execute(
            "UPDATE review_evidence SET state='changed' WHERE id=?",
            (self.evidence[1].id,),
        )
        self.assertEqual(self.context_view().notes, [])

    def test_unknown_and_global_scope_fail_closed(self) -> None:
        self.add()
        unknown = Scope(project_id="unknown", task=None)
        self.assertEqual(
            knowledge_read.search_knowledge(self.conn, "Python", unknown), ()
        )
        self.assertEqual(
            knowledge_push.knowledge_context(
                self.conn, unknown, 1000, 20.0
            ).notes,
            [],
        )
        global_scope = Scope(project_id=None, task=None)
        self.assertEqual(
            knowledge_push.knowledge_context(
                self.conn, global_scope, 1000, 20.0
            ).notes,
            [],
        )

    def test_unverified_and_malformed_support_provenance_is_withheld(
        self,
    ) -> None:
        self.add()
        self.conn.execute("UPDATE source SET replay_mode='unverified'")
        self.assertEqual(self.context_view().notes, [])
        self.conn.execute("UPDATE source SET replay_mode='none'")
        self.conn.execute(
            "UPDATE review_evidence SET original_locator_json='{}'"
        )
        self.assertEqual(self.context_view().notes, [])

    def test_expiry_admission_requires_exact_receipt_and_input(self) -> None:
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        self.set_expiry(ident)
        self.assertEqual(len(self.context_view().notes), 1)
        for sql in (
            "UPDATE review_receipt SET applied_json='[]'",
            "DELETE FROM review_input",
            "DELETE FROM review_receipt",
            "UPDATE review_receipt SET applied_json="
            "json_replace(applied_json,'$[0].revision',2)",
        ):
            with self.subTest(sql=sql):
                self.conn.execute("SAVEPOINT refusal")
                self.conn.execute(sql)
                self.assertEqual(self.context_view().notes, [])
                self.conn.execute("ROLLBACK TO refusal")
                self.conn.execute("RELEASE refusal")

    def test_expiry_rejects_malformed_wrong_scope_and_erased_refs(
        self,
    ) -> None:
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        cases = [
            {"evidence_refs": []},
            {"evidence_refs": ["E_invalid"]},
            {"at": None},
        ]
        for expiry in cases:
            self.set_expiry(ident, **expiry)
            self.assertEqual(self.context_view().notes, [])
        self.set_expiry(ident)
        for value in ("erased", "changed"):
            self.conn.execute(
                "UPDATE review_evidence SET state=? WHERE id=?",
                (value, self.evidence[1].id),
            )
            self.assertEqual(self.context_view().notes, [])
        self.conn.execute(
            "UPDATE review_evidence SET state='live',scope_json=? WHERE id=?",
            (
                canonical_json(Scope(project_id="other", task=None)),
                self.evidence[1].id,
            ),
        )
        self.assertEqual(self.context_view().notes, [])

    def test_project_notes_apply_to_tasks_without_broadening_projects(
        self,
    ) -> None:
        ident = self.add()
        task = Scope(project_id=self.scope.project_id, task="build")
        context = knowledge_push.knowledge_context(self.conn, task, 1000, 20.0)
        self.assertEqual([note.id for note in context.notes], [ident])
        self.assertEqual(
            knowledge_read.knowledge_support(self.conn, ident, 1, task, 20.0),
            (self.evidence[0].id,),
        )

    def test_global_context_requires_explicit_policy_and_owner_support(
        self,
    ) -> None:
        scope = self.global_scope()
        ident = self.add(scope=scope.model_dump())
        self.assertEqual(
            [note.id for note in self.context_view(1000, scope).notes],
            [ident],
        )
        self.assertEqual(
            self.search(),
            (),
        )
        self.conn.execute("UPDATE review_evidence SET authority='reported'")
        self.assertEqual(
            self.context_view(1000, scope).notes,
            [],
        )

    def test_context_candidate_cap_counts_rejections_without_refilling(
        self,
    ) -> None:
        ids = sorted(self.add() for _ in range(6))
        self.add(created_at=30.0, scope={"project_id": "other", "task": None})
        with mock.patch.object(knowledge_push, "_SCAN_CAP", 2):
            context = self.context_view()
            self.assertEqual(
                [note.id for note in context.notes], ids[-2:][::-1]
            )
            self.assertTrue(context.truncated)
            self.conn.execute(
                "DELETE FROM knowledge_support WHERE knowledge_id=?",
                (ids[-1],),
            )
            context = self.context_view()
            self.assertEqual([note.id for note in context.notes], [ids[-2]])
            self.assertTrue(context.truncated)
        self.assertEqual(len(self.search()), 5)
