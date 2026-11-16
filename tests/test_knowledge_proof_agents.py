"""Keep every model-written field of owner knowledge out of agent paths."""

from __future__ import annotations

import json
from typing import Any

from muninn import hook
from tests.test_cli_learning import CliLearningCase

OWNER = "Use Python for this project."
SENTINELS = {
    "content": "SENTINEL_CONTENT zebra",
    "condition": "SENTINEL_CONDITION",
    "reason": "SENTINEL_REASON",
    "expiry": "SENTINEL_EXPIRY",
    "title": "SENTINEL_TITLE zebra",
    "guide_reason": "SENTINEL_GUIDE_REASON",
    "claim": "SENTINEL_CLAIM zebra",
}


class AgentPathTests(CliLearningCase):
    """Show agents the owner proof or a look-up line, never model text."""

    def owner_knowledge(self) -> tuple[str, str]:
        """Save one owner note and one owner guide with sentinel text."""
        note = self.note(
            kind="decision",
            content=SENTINELS["content"],
            conditions=[SENTINELS["condition"]],
            reason=SENTINELS["reason"],
        )
        claim = {
            "text": SENTINELS["claim"],
            "evidence_refs": [self.evidence[0].id],
        }
        guide = {
            "action": "add",
            "form": "guide",
            "kind": "decision",
            "scope": self.scope.model_dump(),
            "title": SENTINELS["title"],
            "summary": claim,
            "applicability": [],
            "steps": [{"key": "one", "claim": claim, "depends_on": []}],
            "decision_points": [],
            "pitfalls": [],
            "reason": SENTINELS["guide_reason"],
        }
        path = self.tmp / "proposal.json"
        path.write_text(
            json.dumps(
                {
                    "request_id": self.context.request_id,
                    "result": self.result([note, guide]).model_dump(),
                }
            )
        )
        code, receipt = self.call("know", "propose", "--input", str(path))
        self.assertEqual(code, 0, receipt)
        ids = [item["knowledge_id"] for item in receipt["applied"]]
        expiry = {
            "at": None,
            "condition": SENTINELS["expiry"],
            "evidence_refs": [self.evidence[0].id],
        }
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=? WHERE knowledge_id=?",
            (json.dumps(expiry), ids[0]),
        )
        return ids[0], ids[1]

    def agent_outputs(self) -> dict[str, str]:
        """Collect raw text from every agent path the CLI and hook give."""
        out: dict[str, str] = {}
        for argv in (
            ("know", "context"),
            ("know", "search", "zebra"),
            ("search", "zebra"),
        ):
            self.call(*argv)
            out[" ".join(argv)] = self.last_stdout
        with self.assertRaises(json.JSONDecodeError):
            self.call("know", "context", "--page", "1")
        out["know context --page 1"] = self.last_stdout
        answer = hook.session_start(
            {
                "hook_event_name": "SessionStart",
                "cwd": str(self.cwd),
                "session_id": "s",
                "source": "startup",
            },
            "claude",
            {"MUNINN_HOME": str(self.home)},
        )
        out["session start"] = json.dumps(answer)
        return out

    def test_no_model_field_reaches_an_agent(self) -> None:
        note, guide = self.owner_knowledge()
        for path, text in self.agent_outputs().items():
            with self.subTest(path=path):
                self.assertIn(note, text)
                self.assertIn(OWNER, text)
                for field, sentinel in SENTINELS.items():
                    self.assertNotIn(sentinel.split()[0], text, field)
        for argv in (("know", "show", note), ("know", "show", guide)):
            self.assertEqual(self.call(*argv)[0], 0)
            self.assertIn("SENTINEL_", self.last_stdout)
            self.assertIn(OWNER, self.last_stdout)

    def test_missing_proof_fails_closed_on_every_path(self) -> None:
        note, guide = self.owner_knowledge()
        # A stored span selector leaves no full owner message to show.
        self.conn.execute(
            "UPDATE review_evidence SET selector_json=json_set(selector_json,"
            "'$.start',0,'$.end',3) WHERE id=?",
            (self.evidence[0].id,),
        )
        outputs = self.agent_outputs()
        for path, text in outputs.items():
            with self.subTest(path=path):
                self.assertNotIn("SENTINEL_", text)
                self.assertNotIn(OWNER, text)
        for ident in (note, guide):
            self.assertIn(
                f"proof unavailable: run muninn know show {ident}",
                outputs["know context"]
                + json.dumps(
                    self.call("know", "search", "zebra")[1], ensure_ascii=False
                ),
            )

    def test_owner_views_keep_the_model_text(self) -> None:
        note, _ = self.owner_knowledge()
        code, shown = self.call("know", "show", note)
        self.assertEqual(code, 0)
        body: dict[str, Any] = shown["body"]
        self.assertEqual(body["content"], SENTINELS["content"])
        self.assertEqual(shown["proof"], [f"[owner] {OWNER}"])
