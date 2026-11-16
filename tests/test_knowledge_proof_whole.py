"""Never show agents part of an owner proof, only all of it or a pointer."""

from __future__ import annotations

import json
import time
import unittest
from typing import Any, cast

from muninn import hook, knowledge_push, query
from muninn.hook_frame import RecallEntry, recall_text
from muninn.knowledge_proof import too_long
from muninn.query import paging
from tests.test_classify import reply, user_msg
from tests.test_review_binding import BindingCase

PROPOSAL = "Use SQLite for the store. " + "Keep the data local. " * 18
CONDITION = "yes, but only on Windows"
MODEL = "Model text zebra."


class WholeProofTests(BindingCase):
    """Keep the condition at the end of a long agreement proof."""

    def test_long_agreement_proof_is_whole_or_a_pointer(self) -> None:
        self.assertGreater(len(PROPOSAL), 400)
        self.conversation(reply(1, PROPOSAL), user_msg(2, CONDITION))
        self.assertEqual(self.decide(MODEL), "completed")
        ident = self.row("SELECT id FROM knowledge")[0]
        pointer = f"proof too long: run muninn know show --proof {ident}"
        assert self.scope.project_id is not None
        env = {"MUNINN_HOME": str(self.home)}
        found = query.search(
            self.conn, "zebra", cwd=self.scope.project_id, env=env
        )
        recall = hook.prompt_submit(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": self.scope.project_id,
                "session_id": "s",
                "prompt": "which zebra storage should we use here",
            },
            "claude",
            env,
        )
        context = knowledge_push.knowledge_context(
            self.conn, self.scope, None, time.time()
        )
        outputs = {
            "search": json.dumps(found["knowledge"]),
            "recall": json.dumps(recall),
            "context": context.model_dump_json(),
        }
        for path, text in outputs.items():
            with self.subTest(path=path):
                self.assertIn(ident, text)
                self.assertNotIn(MODEL, text)
                # The start of the proof only ever comes with its end.
                if "Use SQLite for the store." in text:
                    self.assertIn(CONDITION, text)
                else:
                    self.assertIn(pointer, text)
        self.assertIn(CONDITION, outputs["context"])
        self.assertIn(pointer, outputs["search"])
        self.assertIn(pointer, outputs["recall"])


class ShrinkTests(unittest.TestCase):
    """Replace a proof whole where previews are shortened to fit."""

    def entry(self, text: str, proof: bool) -> dict[str, Any]:
        """Build one knowledge preview."""
        found: dict[str, Any] = {
            "id": "K_" + "a" * 32,
            "revision": 1,
            "state": "active",
            "form": "note",
            "kind": "decision",
            "text": text,
            "scope": {"project_id": "/repo", "task": None},
            "evidence_refs": [],
        }
        return found | {"proof": True} if proof else found

    def test_search_page_gives_the_pointer_not_half_a_proof(self) -> None:
        text = "[owner] " + "word " * 58 + CONDITION
        page: dict[str, Any] = {
            "knowledge": [self.entry(text, True), self.entry(text, False)],
            "hits": [{"snippet": "x" * 5900}],
        }
        paging._shrink_knowledge(page)
        proof, fact = page["knowledge"]
        self.assertEqual(proof["text"], too_long(proof["id"]))
        self.assertNotIn("text_truncated", proof)
        self.assertTrue(fact["text_truncated"])

    def test_recall_cap_gives_the_pointer_not_part_of_a_proof(self) -> None:
        text = "[owner] " + "word " * 40 + CONDITION
        block = recall_text(
            [cast(RecallEntry, self.entry(text, True))], [], []
        )
        self.assertIn(CONDITION, block)
        for limit in range(400, 1200, 40):
            with self.subTest(limit=limit):
                block = recall_text(
                    [cast(RecallEntry, self.entry(text, True))],
                    [],
                    [],
                    limit,
                )
                if "word word" in block:
                    self.assertIn(CONDITION, block)
