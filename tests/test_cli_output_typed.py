"""Protect nested governed text without changing protocol identifiers."""

from __future__ import annotations

import json
import unittest
from typing import Any

from muninn import review_requests
from muninn.cli_output import redacted
from tests.hook_support import HookCliCase

SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"


class TypedOutputTests(unittest.TestCase):
    """Exercise final JSON shaping for current typed content fields."""

    def test_nested_content_lists_and_nullable_text_are_redacted(self) -> None:
        fields = (
            "content",
            "title",
            "reason",
            "conditions",
            "question",
            "excerpt",
            "condition",
            "claim",
            "no_change_reason",
            "retracted_reason",
            "text",
            "quote",
            "preview",
            "first_prompt",
            "retract_reason",
        )
        value: dict[str, Any] = {
            "knowledge": [{"body": dict.fromkeys(fields, SECRET)}],
            "receipt": {"no_change_reason": None},
            "proposal": {"conditions": [SECRET, "ordinary condition"]},
            "detail": {"evidence": [{"excerpt": SECRET}]},
        }
        answer = redacted(value)
        self.assertNotIn(SECRET, json.dumps(answer))
        self.assertEqual(value["detail"]["evidence"][0]["excerpt"], SECRET)
        self.assertEqual(answer["receipt"]["no_change_reason"], None)
        self.assertEqual(
            answer["proposal"]["conditions"],
            ["[redacted:secret]", "ordinary condition"],
        )
        for key in fields:
            self.assertEqual(
                answer["knowledge"][0]["body"][key], "[redacted:secret]"
            )

    def test_identifiers_codes_matchmarks_and_raw_shape_survive(self) -> None:
        value = {
            "id": SECRET,
            "digest": SECRET,
            "state": "active",
            "error": "review_policy_required",
            "schema_version": 1,
            "snippet": "ordinary «match»",
            "raw": SECRET,
            "evidence": [{"id": "E_" + "a" * 32}],
        }
        answer = redacted(value)
        self.assertEqual(
            answer,
            {k: v for k, v in value.items() if k != "raw"}
            | {"raw_redacted": True},
        )
        self.assertEqual(redacted({"raw": "plain"}), {"raw": "plain"})
        split = SECRET[:8] + "«" + SECRET[8:12] + "»" + SECRET[12:]
        self.assertEqual(
            redacted({"snippet": split}), {"snippet": "[redacted:secret]"}
        )


class TypedCommandOutputTests(HookCliCase):
    """Verify the CLI applies redaction to actual typed producer outputs."""

    def test_knowledge_context_proposal_detail_receipt_and_retraction(
        self,
    ) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 13.0
            )
        )
        self.context = self.start()
        note = self.add(
            content=f"Searchable zebra {SECRET}",
            conditions=[SECRET],
            reason=SECRET,
        )
        claim = {"text": SECRET, "evidence_refs": [self.evidence[0].id]}
        guide = self.add(
            form="guide",
            kind="procedure",
            title=SECRET,
            summary=claim,
            applicability=[claim],
            steps=[{"key": "first", "claim": claim, "depends_on": []}],
            decision_points=[claim],
            pitfalls=[claim],
            reason=SECRET,
        )
        for arguments in (
            ("know", "list"),
            ("know", "show", note),
            ("know", "show", guide),
            ("know", "context"),
            ("know", "search", "zebra"),
        ):
            code, out, err = self.muninn(*arguments)
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn(SECRET, json.dumps(out))
            self.assertIn("[redacted:secret]", json.dumps(out))
            self.assertIn(
                note if guide not in arguments else guide, json.dumps(out)
            )
        code, hidden, err = self.muninn("know", "search", SECRET)
        self.assertEqual((code, err, hidden["knowledge"]), (0, "", []))
        proposal = self.tmp / "proposal.json"
        proposal.write_text(
            json.dumps(
                {
                    "request_id": self.context.request_id,
                    "result": self.result([])
                    .model_copy(update={"no_change_reason": SECRET})
                    .model_dump(mode="json"),
                }
            )
        )
        code, receipt, err = self.muninn(
            "know", "propose", "--input", str(proposal)
        )
        self.assertEqual((code, err), (0, ""), receipt)
        self.assertEqual(receipt["no_change_reason"], "[redacted:secret]")
        self.assertEqual(receipt["request_id"], self.context.request_id)
        self.conn.execute("UPDATE review_evidence SET excerpt=?", (SECRET,))
        code, detail, _ = self.muninn("learn", "show", self.context.request_id)
        self.assertEqual(code, 0, detail)
        self.assertNotIn(SECRET, json.dumps(detail))
        self.assertTrue(detail["evidence"])
        self.assertTrue(
            all(
                row["excerpt"] == "[redacted:secret]"
                for row in detail["evidence"]
            )
        )
        self.assertEqual(
            detail["receipt"]["no_change_reason"], "[redacted:secret]"
        )
        code, withdrawn, _ = self.muninn(
            "know",
            "retract",
            note,
            "--expected-revision",
            "1",
            "--reason",
            SECRET,
        )
        self.assertEqual(code, 0, withdrawn)
        self.assertEqual(withdrawn["retracted_reason"], "[redacted:secret]")
        self.assertNotIn(SECRET, json.dumps(withdrawn))
