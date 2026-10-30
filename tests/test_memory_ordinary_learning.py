"""Keep ordinary SessionEnd learning independent of proposal approvals."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

from muninn import cli_hook, review_requests
from muninn.review_models import ReviewContext
from muninn.review_runner import LaunchSpec, RunnerCapability
from tests.ingest_support import TID, line
from tests.test_classify import user_msg
from tests.test_review_supervisor import SupervisorCase


class OrdinaryLearningTests(SupervisorCase):
    """Capture a real hook boundary and commit through a scripted learner."""

    def launch(
        self,
        context: ReviewContext,
        capability: RunnerCapability,
        prompt: bytes,
    ) -> LaunchSpec:
        """Return a synthetic result while retaining the actual worker path."""
        changes = []
        if context.evidence_ids:
            changes.append(
                {
                    "action": "add",
                    "form": "note",
                    "kind": "fact",
                    "scope": context.scope.model_dump(),
                    "content": "Use Python.",
                    "conditions": [],
                    "evidence_refs": [context.evidence_ids[0]],
                    "reason": "The original record supports this note.",
                }
            )
        result = {
            "schema_version": 1,
            "changes": changes,
            "unresolved": [],
            "no_change_reason": None if changes else "No supported change",
        }
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {"op": "final", "result": result},
        }
        self.script = (
            "import json,sys;json.load(sys.stdin);print(json.dumps("
            + repr(envelope)
            + "))"
        )
        return super().launch(context, capability, prompt)

    def test_session_end_saves_ordinary_supported_knowledge_without_d(
        self,
    ) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.request.id, 12.0
            )
        )
        self.records[0]["payload"]["session_id"] = TID
        self.records.append(user_msg(4, "Use Python for this project."))
        self.path.write_bytes(b"".join(map(line, self.records)))
        self.bump(self.path)
        self.run_ingest()
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": TID,
            "cwd": self.scope.project_id,
            "transcript_path": str(self.path),
            "reason": "other",
        }
        env = dict(os.environ) | {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }
        output = io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("sys.stdin", stream),
            redirect_stdout(output),
        ):
            self.assertEqual(
                cli_hook.hook_main(
                    ["session-end", "--provider", "codex"], env
                ),
                0,
            )
        self.assertEqual(output.getvalue(), "{}\n")
        self.assertTrue(
            self.conn.execute(
                "SELECT 1 FROM review_request WHERE trigger='hook'"
            ).fetchone()
        )
        self.finish()
        self.assertGreater(
            self.conn.execute("SELECT count(*) FROM knowledge").fetchone()[0],
            0,
        )
        self.assertGreater(
            self.conn.execute(
                "SELECT count(*) FROM review_receipt WHERE applied_json!='[]'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM memory_candidate"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM memory_decision"
            ).fetchone()[0],
            0,
        )
