"""Redact secrets recognized only after supported text fields are joined."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest import mock

from muninn import review_evidence, store
from muninn.knowledge_models import ReviewResult
from muninn.redaction import redact
from muninn.review_json import canonical_json
from muninn.review_models import Receipt
from tests.ingest_support import rollout
from tests.test_classify import user_msg
from tests.test_cli_learning import CliLearningCase
from tests.test_review_requests import RequestCase

CANARY = "SplitFieldSyntheticCredential0123456789"
KEY = "ghp_structural01234567890123456789"


class KnowledgeStorageProjectionTests(CliLearningCase):
    """Exercise composed note and guide text through real checked writes."""

    def setUp(self) -> None:
        RequestCase.setUp(self)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.tmp / "userhome"),
                    "MUNINN_HOME": str(self.home),
                },
            )
        )
        self.policy = self.policy.model_copy(update={"max_output_bytes": 8192})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.records[1] = user_msg(1, f"Use cache token:\n{CANARY}")
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
        """Keep the checked transaction inside the caller's writer lock."""
        with store.writer_lock(self.home):
            return super().save(result)

    def body(self) -> dict[str, Any]:
        """Read canonical body values directly from the new stored version."""
        return json.loads(
            self.row("SELECT content_json FROM knowledge_version")[0]
        )

    def assert_projection(self, expected: str, canary: str = CANARY) -> None:
        """Check raw storage, exact FTS text and actual public search."""
        self.assertEqual(
            self.row("SELECT text FROM knowledge_fts")[0], expected
        )
        for table in ("knowledge_version", "knowledge_claim", "knowledge_fts"):
            rows = [
                tuple(row)
                for row in self.conn.execute(f"SELECT * FROM {table}")
            ]
            self.assertNotIn(canary, json.dumps(rows))
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM knowledge_fts "
                "WHERE knowledge_fts MATCH ?",
                (f'"{canary}"',),
            )[0],
            0,
        )
        code, matches = self.call("know", "search", canary)
        self.assertEqual((code, matches["knowledge"]), (0, []))

    def test_cli_note_join_redacts_condition_and_search(self) -> None:
        self.assertEqual(
            redact("Use cache token:"), ("Use cache token:", False)
        )
        self.assertEqual(redact(CANARY), (CANARY, False))
        code, receipt = self.propose(
            kind="preference", content="Use cache token:", conditions=[CANARY]
        )
        self.assertEqual((code, receipt["outcome"]), (0, "completed"))
        body = self.body()
        self.assertEqual(body["content"], "Use cache token:")
        self.assertEqual(body["conditions"], ["[redacted:secret]"])
        self.assertEqual(body["evidence_refs"], [self.evidence[0].id])
        self.assertEqual(body["scope"], self.scope.model_dump())
        expected = "Use cache token:\n[redacted:secret]"
        self.assertEqual(
            self.row("SELECT text FROM knowledge_claim")[0], expected
        )
        self.assert_projection(expected)

    def test_condition_join_preserves_unicode_and_internal_newlines(
        self,
    ) -> None:
        self.save(
            self.result(
                [
                    self.note(
                        content="Résumé cache 👣\nUse local mode.",
                        conditions=[
                            "Only with token:\n",
                            f"{CANARY} after setup",
                            "Keep local.",
                        ],
                    )
                ]
            )
        )
        body = self.body()
        self.assertEqual(body["content"], "Résumé cache 👣\nUse local mode.")
        self.assertEqual(
            body["conditions"],
            [
                "Only with token:\n",
                "[redacted:secret] after setup",
                "Keep local.",
            ],
        )
        self.assert_projection(
            "Résumé cache 👣\nUse local mode.\nOnly with token:\n\n"
            "[redacted:secret] after setup\nKeep local."
        )

    def test_guide_uses_fts_order_and_keeps_structural_references(
        self,
    ) -> None:
        def claim(text: str) -> dict[str, object]:
            return {"text": text, "evidence_refs": [self.evidence[0].id]}

        guide = {
            "action": "add",
            "form": "guide",
            "kind": "procedure",
            "scope": self.scope.model_dump(),
            "title": "Cache guide",
            "summary": claim("Overview"),
            "applicability": [claim("Use cache token:")],
            "decision_points": [claim(CANARY)],
            "pitfalls": [claim("Keep local.")],
            "steps": [
                {"key": KEY, "claim": claim("Prepare"), "depends_on": []},
                {"key": "check", "claim": claim("Check"), "depends_on": [KEY]},
            ],
            "reason": "The owner requested this guide.",
        }
        result = self.result([guide])
        original = canonical_json(result)
        self.save(result)
        self.assertEqual(canonical_json(result), original)
        body = self.body()
        expected = {
            key: value for key, value in guide.items() if key != "action"
        }
        expected["decision_points"] = [claim("[redacted:secret]")]
        self.assertEqual(body, expected)
        rows = self.conn.execute("SELECT * FROM knowledge_claim").fetchall()
        self.assertEqual(
            {row["claim_key"]: row["text"] for row in rows},
            {
                "summary": "Overview",
                "applicability/0": "Use cache token:",
                "decision/0": "[redacted:secret]",
                "pitfall/0": "Keep local.",
                f"step/{KEY}": "Prepare",
                "step/check": "Check",
            },
        )
        self.assertEqual(
            json.loads(
                self.row(
                    "SELECT dependency_keys_json FROM knowledge_claim "
                    "WHERE claim_key='step/check'"
                )[0]
            ),
            [f"step/{KEY}"],
        )
        supports = self.conn.execute(
            "SELECT claim_key,evidence_id FROM knowledge_support"
        ).fetchall()
        self.assertEqual(
            {row[0] for row in supports}, {row["claim_key"] for row in rows}
        )
        self.assertEqual({row[1] for row in supports}, {self.evidence[0].id})
        self.assert_projection(
            "Overview\nUse cache token:\n[redacted:secret]\n"
            "Keep local.\nPrepare\nCheck"
        )

    def test_private_key_across_parts_preserves_only_surrounding_prose(
        self,
    ) -> None:
        key = "U3ludGhldGljUHJpdmF0ZUtleTAxMjM0NQ=="
        self.save(
            self.result(
                [
                    self.note(
                        content="Cache prefix -----BEGIN PRIVATE KEY-----",
                        conditions=[
                            key,
                            "-----END PRIVATE KEY----- local suffix",
                        ],
                    )
                ]
            )
        )
        body = self.body()
        self.assertEqual(body["content"], "Cache prefix [redacted:secret]")
        self.assertEqual(
            body["conditions"],
            ["[redacted:secret]", "[redacted:secret] local suffix"],
        )
        self.assert_projection(
            "Cache prefix [redacted:secret]\n[redacted:secret]\n"
            "[redacted:secret] local suffix",
            key,
        )

    def test_plain_multiline_fields_keep_their_original_boundaries(
        self,
    ) -> None:
        change = self.note(
            content="Cache configuration\nOverview",
            conditions=["Only local.\nKeep backups.", "Ask the owner first."],
        )
        self.save(self.result([change]))
        self.assertEqual(
            self.body(),
            {key: value for key, value in change.items() if key != "action"},
        )
        self.assert_projection(
            "Cache configuration\nOverview\nOnly local.\n"
            "Keep backups.\nAsk the owner first."
        )
