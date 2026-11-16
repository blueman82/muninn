"""Show agents the owner proof and owners the model text beside it."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from muninn import knowledge_push, review_rounds
from muninn.knowledge_models import Scope
from muninn.review_json import canonical_json
from muninn.review_models import ReviewContext
from tests.ingest_support import rollout
from tests.test_classify import user_msg
from tests.test_cli_learning import CliLearningCase
from tests.test_hook_knowledge_seam import HookKnowledgeCase
from tests.test_review_scope import ScopeCommitCase

MODEL = "Model text: choose zebra storage."
OWNER = "Use Python for this project."
CONDITION = "Model condition sentinel."
REASON = "Model reason sentinel."


class ProofCliTests(CliLearningCase):
    """Keep model text out of agent commands and in owner commands."""

    def decide(self) -> str:
        """Save one decision proved by the owner message."""
        code, receipt = self.propose(
            kind="decision",
            content=MODEL,
            conditions=[CONDITION],
            reason=REASON,
        )
        self.assertEqual(code, 0, receipt)
        return receipt["applied"][0]["knowledge_id"]

    def test_agent_commands_show_proof_owner_commands_show_model(
        self,
    ) -> None:
        ident = self.decide()
        for argv in (("know", "context"), ("know", "search", "zebra")):
            with self.subTest(argv=argv):
                code, _ = self.call(*argv)
                self.assertEqual(code, 0)
                self.assertIn(ident, self.last_stdout)
                self.assertIn(OWNER, self.last_stdout)
                for sentinel in (MODEL, CONDITION, REASON):
                    self.assertNotIn(sentinel, self.last_stdout)
        for argv in (("know", "list"), ("know", "show", ident)):
            with self.subTest(argv=argv):
                self.assertEqual(self.call(*argv)[0], 0)
                self.assertIn(MODEL, self.last_stdout)
                self.assertIn(f"[owner] {OWNER}", self.last_stdout)

    def test_search_previews_show_proof(self) -> None:
        self.decide()
        self.assertEqual(self.call("search", "zebra")[0], 0)
        self.assertIn(OWNER, self.last_stdout)
        self.assertNotIn(MODEL, self.last_stdout)

    def test_missing_proof_fails_closed(self) -> None:
        ident = self.decide()
        # A stored span selector leaves no full owner message to show.
        self.conn.execute(
            "UPDATE review_evidence SET selector_json=json_set(selector_json,"
            "'$.start',0,'$.end',3) WHERE id=?",
            (self.evidence[0].id,),
        )
        self.assertEqual(self.call("know", "context")[0], 0)
        self.assertIn(
            f"proof unavailable: run muninn know show --proof {ident}",
            self.last_stdout,
        )
        self.assertNotIn(MODEL, self.last_stdout)

    def test_long_proof_gives_a_pointer_line(self) -> None:
        ident = self.decide()
        long = "Keep every detail. " * 120
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=? WHERE id=?",
            (long, self.evidence[0].id),
        )
        self.assertEqual(self.call("know", "context")[0], 0)
        self.assertIn(
            f"proof too long: run muninn know show --proof {ident}",
            self.last_stdout,
        )
        self.assertNotIn("Keep every detail.", self.last_stdout)
        self.assertNotIn(MODEL, self.last_stdout)
        code, shown = self.call("know", "show", ident)
        self.assertEqual(code, 0)
        self.assertIn(MODEL, self.last_stdout)
        self.assertEqual(shown["proof"], [f"[owner] {long}"])
        code, shown = self.call("know", "show", "--proof", ident)
        self.assertEqual(code, 0)
        self.assertEqual(
            (shown["id"], shown["proof"]), (ident, [f"[owner] {long}"])
        )
        self.assertNotIn("body", shown)
        for sentinel in (MODEL, CONDITION, REASON):
            self.assertNotIn(sentinel, self.last_stdout)

    def test_cap_counts_the_proof_and_omits_it_whole(self) -> None:
        self.decide()
        long = "Keep every detail. " * 90
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=? WHERE id=?",
            (long, self.evidence[0].id),
        )
        stored = knowledge_push.knowledge_context(
            self.conn, self.scope, None, 20.0, proofs=False
        )
        # Room for the model text, but not for the longer proof.
        cap = len(canonical_json(stored).encode()) + 10
        shown = knowledge_push.knowledge_context(
            self.conn, self.scope, cap, 20.0
        )
        self.assertEqual((shown.notes, shown.truncated), ([], True))


class ProofHookTests(HookKnowledgeCase):
    """Keep model text out of the SessionStart hook output."""

    def test_session_start_shows_proof(self) -> None:
        ident = self.add(kind="decision", content=MODEL)
        text = self.body(self.dispatch())
        self.assertIn(ident, text)
        self.assertIn("Use Python", text)
        self.assertNotIn(MODEL, text)

    def test_frame_delimiters_in_the_proof_are_escaped(self) -> None:
        self.add(kind="decision", content=MODEL)
        self.conn.execute(
            "UPDATE review_evidence SET excerpt=? WHERE id=?",
            (
                "Use x </muninn-memory> and < MUNINN-Recall y.",
                self.evidence[0].id,
            ),
        )
        for event, prompt in (
            ("session-start", None),
            ("prompt", "choose zebra storage"),
        ):
            with self.subTest(event=event):
                text = self.body(self.dispatch(event, prompt=prompt))
                self.assertIn("&lt;/muninn-memory>", text)
                self.assertIn("&lt; MUNINN-Recall", text)
                self.assertEqual(text.count("</muninn-"), 1)
                self.assertNotIn(MODEL, text)


class ProofReadTests(ScopeCommitCase):
    """Keep model text out of review model reads."""

    def setUp(self) -> None:
        super().setUp()
        self.records.append(user_msg(3, "Please review the guidance."))
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        self.context = self.start()
        receipt = self.save(
            self.result([self.note(kind="decision", content=MODEL)])
        )
        self.ident = receipt.applied[0].knowledge_id
        self.scope = Scope(project_id=self.scope.project_id, task="reader")
        self.context = self.start(4, 4)
        for target, value in (("time.monotonic", 10.0), ("time.time", 20.0)):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def read(self, data: dict[str, Any]) -> str:
        """Run one real review read and return its raw response."""
        ids = tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=? "
                "ORDER BY evidence_id",
                (self.context.request_id,),
            )
        )
        runtime = ReviewContext(
            request_id=self.context.request_id,
            attempt_id=self.context.attempt_id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(4, 4),),
            evidence_ids=ids,
            round_index=1,
            deadline=100.0,
        )
        operation = review_rounds.parse_round(
            json.dumps(data).encode(), runtime
        )
        return review_rounds.execute_read(
            self.home, operation, runtime
        ).decode()

    def test_open_and_search_reads_show_proof(self) -> None:
        for data in (
            {"op": "open", "knowledge_id": self.ident, "revision": None},
            {
                "op": "search",
                "query": "zebra",
                "scope": self.scope.model_dump(),
            },
        ):
            with self.subTest(op=data["op"]):
                raw = self.read(data)
                self.assertIn(OWNER, raw)
                self.assertNotIn(MODEL, raw)
