"""Require immutable normalized candidates and complete verified displays."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from unittest import mock

from pydantic import ValidationError

from muninn import decision_candidates, knowledge_write, store, store_copy
from muninn.decision_models import CandidateInput
from tests.decision_support import DecisionCase


class CandidateTests(DecisionCase):
    """Exercise exact payload retention, original checks and read integrity."""

    def test_canonical_candidate_is_immutable(self) -> None:
        result = self.result([self.note()])
        view = self.stage(result)
        assert view.candidate is not None
        row = self.row("SELECT * FROM memory_candidate")
        payload = json.loads(row["payload_json"])
        expected = hashlib.sha256()
        for part in (
            b"muninn-memory-candidate-v1",
            json.dumps(
                {**payload, "digest": "0" * 64},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode(),
        ):
            expected.update(len(part).to_bytes(8, "big"))
            expected.update(part)
        self.assertEqual(view.digest, expected.hexdigest())
        self.assertEqual(payload, view.candidate.model_dump(mode="json"))
        self.assertEqual(
            payload["result"]["changes"][0]["content"], "Use Python."
        )
        result.changes.clear()
        display = decision_candidates.show_candidate(self.conn, view.id, 65536)
        self.assertEqual(json.loads(display.text)["candidate"], payload)
        self.assertEqual(
            json.loads(display.text)["enrichment"],
            {
                "version": 1,
                "rule": "Append the verified owner evidence ID to every "
                "applicable "
                "claim support, only when absent; preserve original order "
                "and all other fields.",
            },
        )
        for column, value in (
            ("payload_json", "{}"),
            ("digest", "f" * 64),
            ("request_id", "R_" + "f" * 32),
            ("normalization_version", 2),
            ("enrichment_version", 2),
            ("origin_unresolved_id", "U_" + "a" * 32),
        ):
            with (
                self.subTest(column=column),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.conn.execute(
                    f"UPDATE memory_candidate SET {column}=?", (value,)
                )
        fresh = self.stage()
        self.assertNotEqual(fresh.id, view.id)
        self.assertNotEqual(fresh.digest, view.digest)

    def test_incomplete_display_cannot_bind_approval(self) -> None:
        view = self.stage()
        for limit in (1, 0, True):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "^decision_display_limit$"),
            ):
                decision_candidates.show_candidate(self.conn, view.id, limit)
        self.conn.execute(
            "UPDATE memory_candidate SET payload_json=NULL,state='erased'"
        )
        with self.assertRaisesRegex(ValueError, "^decision_erased$"):
            decision_candidates.show_candidate(self.conn, view.id, 65536)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE memory_candidate SET payload_json='{}'")

    def test_digest_binds_every_supported_field_and_versions(self) -> None:
        view = self.stage()
        assert view.candidate is not None
        base = view.candidate.model_dump(mode="json")
        for path, value in (
            (("result", "changes", 0, "kind"), "lesson"),
            (("result", "changes", 0, "reason"), "Another reason"),
            (("result", "changes", 0, "conditions"), ["Only here"]),
            (("result", "changes", 0, "evidence_refs"), [self.evidence[1].id]),
            (("scope", "project_id"), "/other"),
            (("targets",), [{"knowledge_id": "K_" + "a" * 32, "revision": 1}]),
            (("normalization_version",), 2),
            (("enrichment_version",), 2),
            (
                ("disposition",),
                {
                    "unresolved_id": "U_" + "b" * 32,
                    "answer": "Answer",
                    "affected": [],
                    "complete": False,
                },
            ),
        ):
            changed = json.loads(json.dumps(base))
            cursor = changed
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
            self.assertNotEqual(
                decision_candidates.candidate_digest(changed), view.digest
            )
        invalid = self.note(
            expiry={
                "at": 20.0,
                "condition": None,
                "evidence_refs": [self.evidence[0].id],
            }
        )
        with self.assertRaises(ValidationError):
            self.result([invalid])

    def test_normalization_matches_application_and_fts(self) -> None:
        result = self.result(
            [
                self.note(
                    content="Cache prefix -----BEGIN PRIVATE KEY-----",
                    conditions=[
                        "U3ludGhldGljUHJpdmF0ZUtleTAxMjM0NQ==",
                        "-----END PRIVATE KEY----- local suffix",
                    ],
                )
            ]
        )
        view = self.stage(result)
        assert view.candidate is not None
        change = view.candidate.result.changes[0]
        assert change.form == "note"
        self.assertEqual(change.content, "Cache prefix [redacted:secret]")
        self.assertEqual(
            change.conditions,
            ["[redacted:secret]", "[redacted:secret] local suffix"],
        )
        request = self.row(
            "SELECT * FROM review_request WHERE id=?",
            (self.context.request_id,),
        )
        self.conn.execute("BEGIN IMMEDIATE")
        applied = knowledge_write.apply_change(
            self.conn, result.changes[0], request, 15.0
        )
        self.conn.commit()
        body = json.loads(
            self.row(
                "SELECT content_json FROM knowledge_version "
                "WHERE knowledge_id=?",
                (applied.knowledge_id,),
            )[0]
        )
        self.assertEqual(body["content"], change.content)
        self.assertEqual(body["conditions"], change.conditions)
        self.assertNotIn(
            "U3ludGhldGljUHJpdmF0ZUtleTAxMjM0NQ==",
            self.row("SELECT text FROM knowledge_fts")[0],
        )

    def test_stage_requires_transaction_and_rolls_back_completely(
        self,
    ) -> None:
        value = CandidateInput(
            request_id=self.context.request_id,
            result=self.result([self.note()]),
            disposition=None,
        )
        with self.assertRaisesRegex(
            ValueError, "^caller_transaction_required$"
        ):
            decision_candidates.stage_candidate(
                self.conn, self.home, value, 14.0
            )
        before = self.projection()
        self.conn.execute("BEGIN IMMEDIATE")
        with mock.patch.object(
            store, "writer_lock", side_effect=AssertionError("nested root")
        ):
            decision_candidates.stage_candidate(
                self.conn, self.home, value, 14.0
            )
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.projection(), before)

    def test_staging_checks_originals_admission_policy_and_scope(self) -> None:
        for fault, code in (
            ("unknown", "rejected_evidence"),
            ("unadmitted", "rejected_evidence"),
            ("scope", "rejected_scope"),
            ("policy", "rejected_validation"),
            ("changed", "rejected_evidence"),
        ):
            with self.subTest(fault=fault):
                self.conn.execute("BEGIN IMMEDIATE")
                change = self.note()
                old = self.path.read_bytes()
                if fault == "unknown":
                    change["evidence_refs"] = ["E_" + "f" * 32]
                elif fault == "unadmitted":
                    self.conn.execute("DELETE FROM review_input")
                elif fault == "scope":
                    change["scope"] = {"project_id": "/wrong", "task": None}
                elif fault == "policy":
                    self.conn.execute("UPDATE review_control SET admission=0")
                else:
                    old = self.path.read_bytes()
                    self.path.write_bytes(old.replace(b"Python", b"Golang"))
                value = CandidateInput(
                    request_id=self.context.request_id,
                    result=self.result([change]),
                    disposition=None,
                )
                try:
                    with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                        decision_candidates.stage_candidate(
                            self.conn, self.home, value, 14.0
                        )
                    self.assertEqual(
                        self.row("SELECT count(*) FROM memory_candidate")[0], 0
                    )
                finally:
                    self.conn.rollback()
                    if fault == "changed":
                        self.path.write_bytes(old)

    def test_missing_owner_alone_stages_but_later_scope_cannot_hide(
        self,
    ) -> None:
        note = self.note(
            kind="preference", evidence_refs=[self.evidence[1].id]
        )
        self.assertEqual(
            self.stage(self.result([note])).state, "waiting_verification"
        )
        wrong = self.note(scope={"project_id": "/wrong", "task": None})
        with self.assertRaisesRegex(ValueError, "^rejected_scope$"):
            self.stage(self.result([note, wrong]))

    def test_same_v2_copy_preserves_candidate_and_associations(self) -> None:
        self.stage()
        target = store.connect_rw(self.home / "copy.sqlite")
        self.addCleanup(target.close)
        store_copy.copy_governed_state(self.conn, target)
        for table in (
            "memory_candidate",
            "memory_candidate_support",
            "memory_decision",
        ):
            self.assertEqual(
                [
                    tuple(r)
                    for r in self.conn.execute(f"SELECT * FROM {table}")
                ],
                [tuple(r) for r in target.execute(f"SELECT * FROM {table}")],
            )
