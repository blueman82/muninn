"""Keep recognized secrets out of checked knowledge storage and search."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest import mock

from muninn import review_evidence, store
from muninn.knowledge_models import ReviewResult
from muninn.review_json import canonical_json
from muninn.review_models import Receipt
from tests.ingest_support import rollout
from tests.test_classify import user_msg
from tests.test_cli_learning import CliLearningCase
from tests.test_review_requests import RequestCase

CANARY = "sk-StoragePrivacySynthetic0123456789"
STEP_KEY = "ghp_structural01234567890123456789"


class KnowledgeStoragePrivacyTests(CliLearningCase):
    """Exercise real checked writers using disposable selected originals."""

    def setUp(self) -> None:
        RequestCase.setUp(self)
        patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.tmp / "userhome"),
                "MUNINN_HOME": str(self.home),
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.policy = self.policy.model_copy(update={"max_output_bytes": 8192})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.records[1] = user_msg(1, f"Use key {CANARY} for the cache.")
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.evidence = review_evidence.initial_evidence(
            self.home, self.window(), 4096
        ).excerpts
        for record in self.evidence:
            self.persist(record)
        self.context = self.start()
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.conn.execute(
            "INSERT INTO scope_path(cwd,scope_id,method) SELECT ?,id,'cwd' "
            "FROM scope WHERE key=?",
            (str(self.cwd), self.scope.project_id),
        )
        original = self.path.read_bytes()
        self.addCleanup(
            lambda: self.assertEqual(self.path.read_bytes(), original)
        )

    def save(self, result: ReviewResult) -> Receipt:
        """Hold the real writer lock around the checked commit transaction."""
        with store.writer_lock(self.home):
            return super().save(result)

    def value(self, sql: str) -> Any:
        """Read one scalar from the disposable store."""
        return self.row(sql)[0]

    def assert_private_storage(self) -> None:
        """Inspect raw durable text and a direct secret-token FTS lookup."""
        for table in (
            "knowledge",
            "knowledge_version",
            "knowledge_claim",
            "knowledge_fts",
            "unresolved",
            "review_receipt",
        ):
            with self.subTest(table=table):
                rows = [
                    tuple(row)
                    for row in self.conn.execute(f"SELECT * FROM {table}")
                ]
                self.assertNotIn(CANARY, json.dumps(rows))
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM knowledge_fts "
                "WHERE knowledge_fts MATCH ?",
                ('"StoragePrivacySynthetic0123456789"',),
            )[0],
            0,
        )

    def test_cli_note_redacts_version_claim_and_fts(self) -> None:
        evidence_before = self.conn.execute(
            "SELECT * FROM review_evidence"
        ).fetchall()
        code, receipt = self.propose(
            kind="preference",
            content=f"Use key {CANARY} for the cache.",
            conditions=[f"Only with {CANARY}", "Keep the cache local."],
            reason=f"Owner supplied {CANARY}",
        )
        self.assertEqual((code, receipt["outcome"]), (0, "completed"))
        self.assertIsNone(receipt["no_change_reason"])
        ident = receipt["applied"][0]["knowledge_id"]
        version = self.row("SELECT * FROM knowledge_version")
        body = json.loads(version["content_json"])
        self.assertEqual(
            body["content"], "Use key [redacted:secret] for the cache."
        )
        self.assertEqual(
            body["conditions"],
            ["Only with [redacted:secret]", "Keep the cache local."],
        )
        self.assertEqual(body["reason"], "Owner supplied [redacted:secret]")
        self.assertEqual(version["reason"], body["reason"])
        self.assertEqual(
            (body["form"], body["kind"], body["scope"]),
            ("note", "preference", self.scope.model_dump()),
        )
        self.assertEqual(body["evidence_refs"], [self.evidence[0].id])
        expected = (
            "Use key [redacted:secret] for the cache.\n"
            "Only with [redacted:secret]\nKeep the cache local."
        )
        self.assertEqual(
            self.value("SELECT text FROM knowledge_claim"), expected
        )
        self.assertEqual(
            self.value("SELECT text FROM knowledge_fts"), expected
        )
        self.assertEqual(
            self.value("SELECT evidence_id FROM knowledge_support"),
            self.evidence[0].id,
        )
        self.assert_private_storage()
        _, matches = self.call("know", "search", CANARY[3:])
        self.assertEqual(matches["knowledge"], [])
        self.assertEqual(
            self.call("know", "search", "cache")[1]["knowledge"][0]["id"],
            ident,
        )
        self.assertEqual(self.value("SELECT count(*) FROM review_call"), 0)
        self.assertEqual(
            self.conn.execute("SELECT * FROM review_evidence").fetchall(),
            evidence_before,
        )

    def test_guide_redacts_every_claim_but_preserves_structure(self) -> None:
        def claim(label: str, index: int = 0) -> dict[str, object]:
            return {
                "text": f"{label} {CANARY}",
                "evidence_refs": [self.evidence[index].id],
            }

        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": f"Cache guide {CANARY}",
            "summary": claim("Summary"),
            "applicability": [claim("Applicability", 1)],
            "steps": [
                {"key": STEP_KEY, "claim": claim("Prepare"), "depends_on": []},
                {
                    "key": "check",
                    "claim": claim("Check", 1),
                    "depends_on": [STEP_KEY],
                },
            ],
            "decision_points": [claim("Decision")],
            "pitfalls": [claim("Pitfall", 1)],
            "reason": f"Requested guide {CANARY}",
        }
        result = self.result([guide])
        original = canonical_json(result)
        receipt = self.save(result)
        self.assertEqual(canonical_json(result), original)
        version = self.row("SELECT * FROM knowledge_version")
        body = json.loads(version["content_json"])
        self.assertEqual(body["title"], "Cache guide [redacted:secret]")
        self.assertEqual(body["reason"], "Requested guide [redacted:secret]")
        self.assertEqual(version["reason"], body["reason"])
        expected = {
            "summary": ("Summary [redacted:secret]", 0, []),
            "applicability/0": ("Applicability [redacted:secret]", 1, []),
            f"step/{STEP_KEY}": ("Prepare [redacted:secret]", 0, []),
            "step/check": ("Check [redacted:secret]", 1, [f"step/{STEP_KEY}"]),
            "decision/0": ("Decision [redacted:secret]", 0, []),
            "pitfall/0": ("Pitfall [redacted:secret]", 1, []),
        }
        claims = self.conn.execute("SELECT * FROM knowledge_claim").fetchall()
        self.assertEqual({row["claim_key"] for row in claims}, set(expected))
        for row in claims:
            text, index, dependencies = expected[row["claim_key"]]
            self.assertEqual(
                (row["text"], json.loads(row["dependency_keys_json"])),
                (text, dependencies),
            )
            self.assertEqual(
                self.row(
                    "SELECT evidence_id FROM knowledge_support "
                    "WHERE claim_key=?",
                    (row["claim_key"],),
                )[0],
                self.evidence[index].id,
            )
        self.assertEqual(body["summary"]["text"], expected["summary"][0])
        for field, key in (
            ("applicability", "applicability/0"),
            ("decision_points", "decision/0"),
            ("pitfalls", "pitfall/0"),
        ):
            self.assertEqual(body[field][0]["text"], expected[key][0])
        self.assertEqual(body["steps"][0]["key"], STEP_KEY)
        self.assertEqual(body["steps"][1]["depends_on"], [STEP_KEY])
        for step in body["steps"]:
            text, index, _ = expected[f"step/{step['key']}"]
            self.assertEqual(
                step["claim"],
                {"text": text, "evidence_refs": [self.evidence[index].id]},
            )
        self.assertEqual(body["scope"], self.scope.model_dump())
        self.assertEqual(
            self.value("SELECT text FROM knowledge_fts"),
            "\n".join(row["text"] for row in claims),
        )
        self.assertEqual(version["request_id"], receipt.request_id)
        self.assert_private_storage()

    def test_replacement_keeps_sanitized_history_and_current_fts(self) -> None:
        receipt = self.save(
            self.result([self.note(content=f"Original {CANARY}")])
        )
        ident = receipt.applied[0].knowledge_id
        old = self.value("SELECT content_json FROM knowledge_version")
        self.context = self.start(2, 2)
        change = self.note(
            action="replace",
            knowledge_id=ident,
            expected_revision=1,
            content=f"Replacement {CANARY}",
            conditions=[f"If {CANARY}"],
            reason=f"Revised {CANARY}",
        )
        receipt = self.save(self.result([change]))
        self.assertEqual(
            (receipt.applied[0].knowledge_id, receipt.applied[0].revision),
            (ident, 2),
        )
        versions = self.conn.execute(
            "SELECT revision,content_json FROM knowledge_version "
            "ORDER BY revision"
        ).fetchall()
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0][:], (1, old))
        self.assertEqual(
            json.loads(old)["content"], "Original [redacted:secret]"
        )
        body = json.loads(versions[1]["content_json"])
        self.assertEqual(
            (body["content"], body["reason"]),
            ("Replacement [redacted:secret]", "Revised [redacted:secret]"),
        )
        self.assertEqual(
            self.row("SELECT current_revision,state FROM knowledge")[:],
            (2, "active"),
        )
        self.assertEqual(
            self.row("SELECT revision,text FROM knowledge_fts")[:],
            (2, "Replacement [redacted:secret]\nIf [redacted:secret]"),
        )
        self.assert_private_storage()

    def test_unresolved_redacts_text_and_withholds(
        self,
    ) -> None:
        affected = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(2, 2)
        proposal = {
            "claim": f"Uncertain {CANARY}",
            "question": f"Which cache uses {CANARY}?",
            "affected": [affected.model_dump()],
            "evidence_refs": [self.evidence[0].id],
        }
        receipt = self.save(self.result([], [proposal]))
        row = self.row("SELECT * FROM unresolved")
        body = json.loads(row["item_json"])
        self.assertEqual(
            body,
            {
                **proposal,
                "claim": "Uncertain [redacted:secret]",
                "question": "Which cache uses [redacted:secret]?",
            },
        )
        self.assertEqual(
            json.loads(row["affected_json"]), proposal["affected"]
        )
        self.assertEqual(
            json.loads(row["support_json"]), proposal["evidence_refs"]
        )
        self.assertEqual(
            (row["id"], row["request_id"]),
            (receipt.unresolved_ids[0], receipt.request_id),
        )
        self.assertEqual(
            (receipt.outcome, receipt.no_change_reason),
            ("completed_unresolved", None),
        )
        self.assertEqual(self.value("SELECT state FROM knowledge"), "withheld")
        self.assertEqual(
            self.value("SELECT state FROM knowledge_claim"), "withheld"
        )
        self.assertEqual(self.value("SELECT count(*) FROM knowledge_fts"), 0)
        self.assert_private_storage()

    def test_cli_retraction_redacts_reason_after_revision_check(self) -> None:
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        old = self.value("SELECT content_json FROM knowledge_version")
        self.context = self.start(2, 2)
        args = (
            "know",
            "retract",
            ident,
            "--reason",
            f"Withdraw {CANARY}",
            "--expected-revision",
        )
        code, out = self.call(*args, "2")
        self.assertEqual((code, out["error"]), (2, "rejected_revision"))
        self.assertEqual(
            self.row("SELECT state,retracted_reason FROM knowledge")[:],
            ("active", None),
        )
        code, out = self.call(*args, "1")
        self.assertEqual(
            (code, out["state"], out["retracted_reason"]),
            (0, "retracted", "Withdraw [redacted:secret]"),
        )
        self.assertEqual(
            self.value("SELECT retracted_reason FROM knowledge"),
            "Withdraw [redacted:secret]",
        )
        self.assertEqual(
            self.value("SELECT content_json FROM knowledge_version"), old
        )
        self.assertEqual(
            self.row(
                "SELECT state FROM review_request WHERE id=?",
                (self.context.request_id,),
            )[0],
            "cancelled",
        )
        self.assertEqual(self.value("SELECT count(*) FROM knowledge_fts"), 0)
        self.assert_private_storage()

    def test_no_change_reason_matches_storage_audit_and_retry(self) -> None:
        result = self.result([]).model_copy(
            update={"no_change_reason": f"Already known {CANARY}"}
        )
        receipt = self.save(result)
        self.assertEqual(
            (receipt.outcome, receipt.no_change_reason),
            ("no_change", "Already known [redacted:secret]"),
        )
        self.assertEqual(
            self.value("SELECT no_change_reason FROM review_receipt"),
            receipt.no_change_reason,
        )
        code, detail = self.call("learn", "show", receipt.request_id)
        self.assertEqual(
            (code, detail["receipt"]), (0, receipt.model_dump(mode="json"))
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        self.assertEqual(self.save(self.result([self.note()])), receipt)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 1))
        self.assert_private_storage()

    def test_legitimate_prose_and_null_reason_stay_intact(
        self,
    ) -> None:
        change = self.note(
            content="Authorization: basic configuration",
            conditions=["Only for local caches."],
            reason="The owner requested this.",
        )
        receipt = self.save(self.result([change]))
        body = json.loads(
            self.value("SELECT content_json FROM knowledge_version")
        )
        self.assertEqual(
            body,
            {key: value for key, value in change.items() if key != "action"},
        )
        self.assertIsNone(receipt.no_change_reason)
        self.assertIsNone(
            self.value("SELECT no_change_reason FROM review_receipt")
        )
        self.context = self.start(2, 2)
        self.assertEqual(
            self.save(self.result([])).no_change_reason, "No change."
        )

    def test_original_result_size_is_checked_before_storage(
        self,
    ) -> None:
        result = self.result([self.note(content="sk-" + "X" * 9000)])
        with self.assertRaisesRegex(ValueError, "rejected_validation"):
            self.save(result)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertFalse(self.conn.in_transaction)
