"""Exercise current decision relationships with real SQLite constraints."""

from __future__ import annotations

import sqlite3

from muninn import decision_candidates, store, store_copy
from muninn.decision_models import DecisionView
from tests.decision_support import DecisionCase


class DecisionSchemaTests(DecisionCase):
    """Separate ordinary request identities and accepted decision revisions."""

    def accepted(
        self,
        view: DecisionView,
        revision: int | float = 1,
        action: str = "approve",
    ) -> None:
        """Insert synthetic accepted identities for SQL constraint tests."""
        self.conn.execute(
            "INSERT INTO memory_decision(id,candidate_id,candidate_digest,"
            "intent_key,action,locator_json,submitted_at,state,accepted_revision,"
            "owner_evidence_id,display_evidence_id,accepted_at) "
            "VALUES(?,?,?,?,?,'{}',14.0,'verified',?,?,?,14.0)",
            (
                f"intent-{revision}",
                view.id,
                view.digest,
                f"key-{revision}",
                action,
                revision,
                self.evidence[0].id,
                self.evidence[1].id,
            ),
        )

    def application(
        self, ident: str, candidate: str | None, revision: int | float | None
    ) -> None:
        """Insert a distinct R over the original ordinary window and policy."""
        self.conn.execute(
            "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,trigger,origin,state,created_at,"
            "candidate_id,decision_revision) SELECT ?,?,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,'explicit','proposal','pending',"
            "15.0,?,? "
            "FROM review_request WHERE id=?",
            (ident, ident, candidate, revision, self.context.request_id),
        )

    def test_application_pair_requires_actual_accepted_approval(self) -> None:
        view = self.stage()
        for candidate, revision in (
            (view.id, None),
            (None, 1),
            (view.id, 0),
            (view.id, 1),
        ):
            with (
                self.subTest(candidate=candidate, revision=revision),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.application("R_" + "a" * 32, candidate, revision)
        self.accepted(view)
        self.application("R_" + "a" * 32, view.id, 1)
        for ident, candidate, revision in (
            ("R_" + "b" * 32, view.id, 1),
            ("R_" + "b" * 32, None, None),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.application(ident, candidate, revision)
        self.accepted(view, 2, "reject")
        with self.assertRaises(sqlite3.IntegrityError):
            self.application("R_" + "c" * 32, view.id, 2)
        for sql in (
            "UPDATE review_request SET decision_revision=2 "
            "WHERE candidate_id IS NOT NULL",
            "UPDATE memory_decision SET accepted_revision=3 "
            "WHERE accepted_revision=1",
            "UPDATE memory_decision SET owner_evidence_id=NULL "
            "WHERE accepted_revision=1",
            "DELETE FROM memory_decision WHERE accepted_revision=1",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.conn.execute(sql)

    def test_decision_binding_refuses_wrong_digest_and_noninteger_revision(
        self,
    ) -> None:
        view = self.stage()
        with self.assertRaises(sqlite3.IntegrityError):
            self.accepted(view, 1.5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.accepted(view.model_copy(update={"digest": "f" * 64}))

    def test_ordinary_dedup_and_public_view_are_unchanged(self) -> None:
        ordinary = self.save(self.result([self.note()]))
        before = tuple(self.row("SELECT * FROM review_receipt"))
        request = self.enqueue()
        key = self.row("SELECT duplicate_key FROM review_request")[0]
        self.assertEqual(request.id, ordinary.request_id)
        self.assertNotIn("candidate_id", request.model_dump())
        self.assertNotIn("decision_revision", request.model_dump())
        self.stage()
        request_again = self.enqueue()
        self.assertEqual(request_again.id, request.id)
        self.assertEqual(
            self.row("SELECT duplicate_key FROM review_request")[0], key
        )
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_receipt")), before
        )
        self.assertEqual(
            self.row(
                "SELECT candidate_id,decision_revision FROM review_request"
            )[0],
            None,
        )

    def test_copy_retains_pending_and_accepted_history_and_all_support_roles(
        self,
    ) -> None:
        view = self.stage()
        self.accepted(view)
        self.application("R_" + "a" * 32, view.id, 1)
        self.accepted(view, 2, "reject")
        self.conn.execute(
            "UPDATE memory_candidate SET state='rejected',revision=2,"
            "active_decision_id='intent-2'"
        )
        self.conn.execute(
            "INSERT INTO memory_decision(id,candidate_id,candidate_digest,"
            "intent_key,action,locator_json,submitted_at,state) "
            "VALUES('pending',?,?,'pending','reject','{}',15.0,'waiting_verification')",
            (view.id, view.digest),
        )
        self.conn.executemany(
            "INSERT INTO memory_candidate_support VALUES(?,?,?)",
            [
                (view.id, self.evidence[0].id, "owner"),
                (view.id, self.evidence[1].id, "display"),
            ],
        )
        target = store.connect_rw(self.home / "copy.sqlite")
        self.addCleanup(target.close)
        store_copy.copy_governed_state(self.conn, target)
        for table in (
            "memory_candidate",
            "memory_decision",
            "memory_candidate_support",
        ):
            self.assertEqual(
                [
                    tuple(r)
                    for r in self.conn.execute(f"SELECT * FROM {table}")
                ],
                [tuple(r) for r in target.execute(f"SELECT * FROM {table}")],
            )
        status = decision_candidates.candidate_view(self.conn, view.id)
        self.assertEqual(status.state, "rejected")
        self.assertEqual(status.intent_state, "waiting_verification")

    def test_erasure_clears_bodies_and_evidence_without_rewriting_history(
        self,
    ) -> None:
        view = self.stage()
        self.accepted(view)
        self.application("R_" + "b" * 32, view.id, 1)
        self.conn.execute(
            "UPDATE memory_candidate SET state='erased',payload_json=NULL,"
            "derived_digest=NULL,application_request_id=NULL"
        )
        self.conn.execute(
            "UPDATE memory_decision SET state='erased',locator_json=NULL,"
            "owner_evidence_id=NULL,display_evidence_id=NULL"
        )
        self.conn.execute("DELETE FROM memory_candidate_support")
        row = self.row(
            "SELECT accepted_revision,candidate_digest FROM memory_decision"
        )
        self.assertEqual(tuple(row), (1, view.digest))
        self.assertIsNone(
            decision_candidates.candidate_view(self.conn, view.id).candidate
        )
        target = store.connect_rw(self.home / "erased-copy.sqlite")
        self.addCleanup(target.close)
        store_copy.copy_governed_state(self.conn, target)
        self.assertEqual(
            [
                tuple(r)
                for r in self.conn.execute("SELECT * FROM review_request")
            ],
            [tuple(r) for r in target.execute("SELECT * FROM review_request")],
        )
