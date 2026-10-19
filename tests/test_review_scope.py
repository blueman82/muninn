"""Check scope containment through selected originals and checked commits."""

from __future__ import annotations

import unittest

from muninn import review_evidence, review_requests
from muninn.knowledge_models import Scope
from muninn.knowledge_scope import scope_applies
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_json import canonical_json
from tests.ingest_support import rollout
from tests.test_classify import user_msg
from tests.test_review_commit import CommitCase
from tests.test_review_requests import RequestCase


class ScopeCommitCase(CommitCase):
    """Select physical originals before admitting a requested scope."""

    def setUp(self) -> None:
        RequestCase.setUp(self)
        self.records[1] = user_msg(1, "Use Python for this project.")
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.select_originals()

    def select_originals(self) -> None:
        """Persist exact selected records with their original mapped scopes."""
        selected = []
        for number in (2, 3):
            window = self.window(number, number)
            record = review_evidence.select_evidence(
                self.home,
                EvidenceLocator(
                    source=window.source,
                    line=number,
                    part=1,
                    line_sha256=window.boundary_hash,
                ),
                EvidenceSelector(
                    json_pointer="/payload/content/0/text",
                    start=None,
                    end=None,
                ),
            )
            self.persist(record)
            selected.append(record)
        self.evidence = tuple(selected)


class ReviewScopeCommitTests(ScopeCommitCase):
    """Keep request authority distinct from unchanged source identity."""

    def test_selected_project_evidence_claims_and_commits_narrow_task(
        self,
    ) -> None:
        source_scope = self.scope
        selected = tuple(record.model_dump_json() for record in self.evidence)
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        try:
            self.context = self.start()
        except ValueError as exc:
            self.fail(f"Project evidence must admit a narrower task: {exc}")
        self.conn.execute(
            "UPDATE review_source SET activation_line=1,completed_line=1"
        )
        receipt = self.save(self.result([self.note()]))
        self.assertEqual(receipt.outcome, "completed")
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))
        self.assertEqual(
            self.row("SELECT scope_json FROM knowledge_version")[0],
            canonical_json(self.scope),
        )
        self.assertEqual(
            self.row("SELECT scope_json,state FROM review_request")[:],
            (canonical_json(self.scope), "completed"),
        )
        checked = review_evidence.validate_evidence(
            self.conn,
            self.home,
            tuple(record.id for record in self.evidence),
            self.scope,
            self.policy,
        )
        self.assertEqual(
            tuple(record.model_dump_json() for record in checked), selected
        )
        self.assertTrue(
            all(record.scope == source_scope for record in checked)
        )
        self.assertEqual(
            self.row("SELECT completed_line FROM review_source")[0], 3
        )

    def test_exact_project_still_commits(self) -> None:
        self.context = self.start()
        self.assertEqual(
            self.save(self.result([self.note()])).outcome, "completed"
        )
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))

    def test_body_scope_cannot_override_requested_task(self) -> None:
        project = self.scope
        self.scope = Scope(project_id=project.project_id, task="parser")
        self.context = self.start()
        before = self.row("SELECT completed_line FROM review_source")[0]
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.save(self.result([self.note(scope=project.model_dump())]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            self.row("SELECT completed_line FROM review_source")[0], before
        )

    def test_another_request_does_not_admit_project_support(self) -> None:
        self.context = self.start(2, 2)
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 13.0
            )
        )
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start(3, 3)
        before = self.row("SELECT completed_line FROM review_source")[0]
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([self.note()]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            self.row("SELECT completed_line FROM review_source")[0], before
        )

    def test_inapplicable_stored_scope_cannot_commit(self) -> None:
        project = self.scope.project_id
        for source, target in (
            (
                Scope(project_id="other", task=None),
                Scope(project_id=project, task="parser"),
            ),
            (
                Scope(project_id=project, task="parser"),
                Scope(project_id=project, task="writer"),
            ),
            (
                Scope(project_id=project, task="parser"),
                Scope(project_id=project, task=None),
            ),
            (
                Scope(project_id=None, task=None),
                Scope(project_id=project, task=None),
            ),
        ):
            with self.subTest(source=source, target=target):
                case = ScopeCommitCase()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.scope = target
                case.context = case.start()
                case.conn.execute(
                    "UPDATE review_evidence SET scope_json=?",
                    (canonical_json(source),),
                )
                before = case.row("SELECT completed_line FROM review_source")[
                    0
                ]
                with self.assertRaisesRegex(ValueError, "rejected_evidence"):
                    case.save(case.result([case.note()]))
                self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))
                self.assertEqual(
                    case.row("SELECT completed_line FROM review_source")[0],
                    before,
                )

    def test_relabeling_source_as_requested_task_fails_original_match(
        self,
    ) -> None:
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        self.conn.execute(
            "UPDATE review_evidence SET scope_json=?",
            (canonical_json(self.scope),),
        )
        with self.assertRaisesRegex(ValueError, "evidence_changed"):
            review_evidence.validate_evidence(
                self.conn,
                self.home,
                self.context.evidence_ids,
                self.scope,
                self.policy,
            )
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([self.note()]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_project_evidence_cannot_widen_to_global(self) -> None:
        self.policy = self.policy.model_copy(update={"allow_global": True})
        self.scope = Scope(project_id=None, task=None)
        with self.assertRaisesRegex(ValueError, "evidence_unavailable"):
            review_evidence.validate_evidence(
                self.conn,
                self.home,
                tuple(record.id for record in self.evidence),
                self.scope,
                self.policy,
            )
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.enqueue()
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))


class GlobalScopeTests(ScopeCommitCase):
    """Preserve separate active policy and attributable owner requirements."""

    def setUp(self) -> None:
        RequestCase.setUp(self)
        self.records[1] = user_msg(1, "Use Python for this project.")
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.conn.execute(
            "UPDATE scope SET key='global',kind='global' WHERE key=?",
            (self.scope.project_id,),
        )
        self.scope = Scope(project_id=None, task=None)
        self.select_originals()

    def allow_global(self) -> None:
        """Enable the explicit policy in both snapshot and active control."""
        self.policy = self.policy.model_copy(update={"allow_global": True})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def test_exact_global_relation_does_not_bypass_policy(self) -> None:
        self.assertTrue(scope_applies(self.evidence[0].scope, self.scope))
        with self.assertRaisesRegex(ValueError, "evidence_policy"):
            review_evidence.validate_evidence(
                self.conn,
                self.home,
                (self.evidence[0].id,),
                self.scope,
                self.policy,
            )
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.enqueue()
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_enabled_global_still_requires_owner_support(self) -> None:
        self.allow_global()
        self.context = self.start()
        self.assertEqual(self.evidence[1].authority, "reported")
        before = self.row("SELECT completed_line FROM review_source")[0]
        with self.assertRaisesRegex(ValueError, "rejected_authority"):
            self.save(
                self.result([self.note(evidence_refs=[self.evidence[1].id])])
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            self.row("SELECT completed_line FROM review_source")[0], before
        )

    def test_enabled_global_with_owner_support_commits(self) -> None:
        self.allow_global()
        self.context = self.start()
        self.assertEqual(self.evidence[0].authority, "direct_owner")
        self.assertEqual(
            self.save(self.result([self.note()])).outcome, "completed"
        )
        self.assertEqual(self.counts(), (1, 1, 1, 1, 0, 1, 1))

    def test_global_commit_requires_current_enabled_policy(self) -> None:
        self.allow_global()
        self.context = self.start()
        self.policy = self.policy.model_copy(update={"allow_global": False})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        with self.assertRaisesRegex(ValueError, "rejected_validation"):
            self.save(self.result([self.note()]))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))


class ScopeRelationTests(unittest.TestCase):
    """Reject widening and cross-project inheritance in the shared relation."""

    def test_scope_relation_matrix(self) -> None:
        scopes = (
            Scope(project_id="one", task=None),
            Scope(project_id="one", task="parser"),
            Scope(project_id="one", task="writer"),
            Scope(project_id="two", task=None),
            Scope(project_id="two", task="parser"),
            Scope(project_id=None, task=None),
        )
        expected = (
            (True, True, True, False, False, False),
            (False, True, False, False, False, False),
            (False, False, True, False, False, False),
            (False, False, False, True, True, False),
            (False, False, False, False, True, False),
            (False, False, False, False, False, True),
        )
        for source, row in zip(scopes, expected, strict=True):
            for target, applicable in zip(scopes, row, strict=True):
                with self.subTest(source=source, target=target):
                    self.assertIs(scope_applies(source, target), applicable)
