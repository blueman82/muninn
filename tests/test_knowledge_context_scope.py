"""Join knowledge retrieval with physical selection and checked commit."""

from __future__ import annotations

from muninn import knowledge_read, review_evidence
from muninn.knowledge_models import Scope
from muninn.review_json import canonical_json
from tests.knowledge_context_support import KnowledgeScopeCase


class KnowledgeScopeTests(KnowledgeScopeCase):
    """Keep narrowed task use consistent across claims, commits and reads."""

    def test_project_support_commits_and_reads_narrow_task_guidance(
        self,
    ) -> None:
        original = tuple(record.model_dump_json() for record in self.evidence)
        task = Scope(project_id=self.scope.project_id, task="parser")
        self.scope = task
        self.context = self.start()
        receipt = self.save(self.result([self.note()]))
        ident = receipt.applied[0].knowledge_id
        self.assertEqual(receipt.outcome, "completed")
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))
        self.assertEqual(
            self.row("SELECT scope_json FROM knowledge_version")[0],
            canonical_json(task),
        )
        self.assertEqual(
            [note.id for note in self.context_for(task).notes], [ident]
        )
        self.assertEqual([note.id for note in self.search_for(task)], [ident])
        supports = knowledge_read.knowledge_support(
            self.conn, ident, 1, task, 20.0
        )
        self.assertEqual(supports, (self.evidence[0].id,))
        checked = review_evidence.validate_evidence(
            self.conn, self.home, supports, task, self.policy
        )
        self.assertEqual(checked[0].model_dump_json(), original[0])
        self.assertEqual(checked[0].scope, self.project_scope)
        self.assertEqual(
            Scope.model_validate_json(
                self.row(
                    "SELECT scope_json FROM review_evidence WHERE id=?",
                    (supports[0],),
                )[0]
            ),
            self.project_scope,
        )
        self.assertEqual(
            tuple(record.model_dump_json() for record in self.evidence),
            original,
        )

    def test_retrieved_project_support_is_admitted_to_actual_task_request(
        self,
    ) -> None:
        original = self.evidence[0].model_dump_json()
        self.context = self.start(2, 2)
        project_receipt = self.save(self.result([self.note()]))
        project_id = project_receipt.applied[0].knowledge_id
        task = Scope(project_id=self.scope.project_id, task="parser")
        self.assertEqual(
            [note.id for note in self.context_for(task).notes], [project_id]
        )
        self.scope = task
        self.context = self.start(3, 3)
        self.assertNotIn(self.evidence[0].id, self.context.evidence_ids)
        supports = self.admit_support(project_id)
        self.assertEqual(supports, (self.evidence[0].id,))
        self.assertIn(supports[0], self.context.evidence_ids)
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_input WHERE request_id=?",
                (self.context.request_id,),
            )[0],
            2,
        )
        task_receipt = self.save(self.result([self.note()]))
        task_id = task_receipt.applied[0].knowledge_id
        self.assertEqual(task_receipt.outcome, "completed")
        self.assertEqual(
            {note.id for note in self.context_for(task).notes},
            {project_id, task_id},
        )
        self.assertEqual(
            {note.id for note in self.search_for(task)}, {project_id, task_id}
        )
        self.assertEqual(
            knowledge_read.knowledge_support(
                self.conn, task_id, 1, task, 20.0
            ),
            supports,
        )
        self.assertEqual(self.evidence[0].model_dump_json(), original)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 2)

    def test_task_guidance_never_widens_to_project_other_task_or_global(
        self,
    ) -> None:
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        self.conn.execute(
            "INSERT INTO scope(key,label,kind) VALUES('other','Other','dir')"
        )
        for target in (
            self.project_scope,
            Scope(project_id=self.scope.project_id, task="writer"),
            Scope(project_id="other", task="parser"),
            Scope(project_id=None, task=None),
        ):
            with self.subTest(target=target):
                self.assertEqual(self.context_for(target).notes, [])
                self.assertEqual(self.search_for(target), ())
                with self.assertRaisesRegex(
                    ValueError, "knowledge_ineligible"
                ):
                    knowledge_read.knowledge_support(
                        self.conn, ident, 1, target, 20.0
                    )
        self.assertEqual(
            [note.id for note in self.context_for(self.scope).notes], [ident]
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 1)

    def test_inapplicable_support_scope_cannot_authorize_body_scope(
        self,
    ) -> None:
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        for source in (
            Scope(project_id=self.scope.project_id, task="parser"),
            Scope(project_id="other", task=None),
            Scope(project_id=None, task=None),
        ):
            with self.subTest(source=source):
                self.conn.execute(
                    "UPDATE review_evidence SET scope_json=? WHERE id=?",
                    (canonical_json(source), self.evidence[0].id),
                )
                self.assertEqual(
                    self.context_for(self.project_scope).notes, []
                )
                with self.assertRaisesRegex(
                    ValueError, "knowledge_ineligible"
                ):
                    knowledge_read.knowledge_support(
                        self.conn, ident, 1, self.project_scope, 20.0
                    )
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 1)

    def test_model_scope_cannot_broaden_the_task_request(self) -> None:
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.save(
                self.result([self.note(scope=self.project_scope.model_dump())])
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.context_for(self.scope).notes, [])

    def test_global_retrieval_requires_current_policy_and_owner_authority(
        self,
    ) -> None:
        self.global_originals()
        self.global_policy(enabled=True)
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        self.assertEqual(
            [note.id for note in self.context_for(self.scope).notes], [ident]
        )
        self.assertEqual(
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            ),
            (self.evidence[0].id,),
        )
        self.global_policy(enabled=False)
        self.assertEqual(self.context_for(self.scope).notes, [])
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.scope, 20.0
            )
        self.global_policy(enabled=True)
        self.conn.execute(
            "UPDATE review_evidence SET authority='reported' WHERE id=?",
            (self.evidence[0].id,),
        )
        self.assertEqual(self.context_for(self.scope).notes, [])
        self.assertEqual(self.search_for(self.scope), ())

    def test_global_scope_equality_does_not_grant_commit_permission(
        self,
    ) -> None:
        self.global_originals()
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.enqueue()
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.global_policy(enabled=True)
        self.context = self.start()
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.save(
                self.result([self.note(evidence_refs=[self.evidence[1].id])])
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_project_guidance_cannot_widen_under_enabled_global_policy(
        self,
    ) -> None:
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        self.global_policy(enabled=True)
        target = Scope(project_id=None, task=None)
        self.assertTrue(self.policy.allow_global)
        self.assertEqual(self.context_for(target).notes, [])
        self.assertEqual(self.search_for(target), ())
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(self.conn, ident, 1, target, 20.0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 1)

    def test_global_guidance_does_not_inherit_into_a_mapped_project(
        self,
    ) -> None:
        self.global_originals()
        self.global_policy(enabled=True)
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        self.conn.execute(
            "INSERT INTO scope(key,label,kind) VALUES(?, 'Project','dir')",
            (self.project_scope.project_id,),
        )
        self.assertEqual(self.context_for(self.project_scope).notes, [])
        self.assertEqual(self.search_for(self.project_scope), ())
        with self.assertRaisesRegex(ValueError, "knowledge_ineligible"):
            knowledge_read.knowledge_support(
                self.conn, ident, 1, self.project_scope, 20.0
            )
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 1)
