"""Show fixed learning causes in hooks and optional health reports."""

from __future__ import annotations

import os

from muninn import hook, obs, review_recovery
from tests.ingest_support import TID
from tests.test_review_requests import RequestCase


class LearningMessageTests(RequestCase):
    """Expose required action without a manual health command."""

    def test_next_session_shows_action_in_every_recall_mode(self) -> None:
        self.conn.execute(
            "INSERT INTO scope_path(cwd,scope_id,method) "
            "SELECT ?,id,'cwd' FROM scope WHERE key=?",
            (str(self.tmp), self.scope.project_id),
        )
        self.transaction(
            lambda: review_recovery.record(
                self.conn, "setup", "auth", "launch", 20.0
            )
        )
        env = dict(os.environ) | {
            "MUNINN_HOME": str(self.home),
            "PWD": str(self.tmp),
        }
        for mode in ("recall.off", "recall.advisory"):
            marker = self.home / mode
            marker.touch()
            trace: dict[str, object] = {}
            shown = hook.session_start(
                {
                    "session_id": TID,
                    "cwd": str(self.tmp),
                    "transcript_path": str(self.path),
                },
                "codex",
                env,
                trace=trace,
            )
            message = shown["systemMessage"]
            assert isinstance(message, str)
            self.assertIn("Open Codex and sign in.", message)
            self.assertNotIn("learning_notice", trace)
            marker.unlink()

    def test_doctor_explains_the_safe_stored_cause(self) -> None:
        self.transaction(
            lambda: review_recovery.record(
                self.conn, "setup", "auth", "launch", 20.0
            )
        )
        checks = {
            item["check"]: item for item in obs.review_checks(self.conn, 21.0)
        }
        self.assertIn(
            "Codex sign-in is unavailable.",
            checks["review_attention"]["detail"],
        )
