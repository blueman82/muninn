"""Check task-scoped learning requests against disposable indexed evidence."""

from __future__ import annotations

from muninn import review_requests
from muninn.knowledge_models import Scope
from tests.test_cli_learning import CliLearningCase


class TaskRequestTests(CliLearningCase):
    """Keep request scope tied to the physical source and proposal scope."""

    def request(self, task: str) -> tuple[int, dict[str, object]]:
        """Request one bounded interval for a named task."""
        source = self.window().source
        return self.call(
            "learn",
            "request",
            "--session",
            f"{source.provider}:{source.thread_id}",
            "--from-line",
            "2",
            "--through-line",
            "3",
            "--task",
            task,
        )

    def test_task_request_deduplicates_and_rejects_wrong_task_proposal(
        self,
    ) -> None:
        """Only an exact task proposal can create task guidance."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        code, request = self.request("parser")
        self.assertEqual(code, 0)
        expected_scope = {
            "project_id": self.scope.project_id,
            "task": "parser",
        }
        self.assertEqual(request["scope"], expected_scope)
        code, duplicate = self.request("parser")
        self.assertEqual((code, duplicate["id"]), (0, request["id"]))
        self.assertEqual(self.row("SELECT count(*) FROM review_request")[0], 2)
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        self.assertEqual(self.context.request_id, request["id"])
        code, refused = self.propose(
            scope={"project_id": self.scope.project_id, "task": "writer"}
        )
        self.assertEqual((code, refused["error"]), (2, "rejected_scope"))
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_task_request_is_read_only_in_its_exact_task(self) -> None:
        """A committed task note stays outside other task reads."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        code, request = self.request("parser")
        self.assertEqual(code, 0)
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        self.assertEqual(self.context.request_id, request["id"])
        code, receipt = self.propose()
        self.assertEqual(code, 0)
        ident = receipt["applied"][0]["knowledge_id"]
        for task, expected in (("parser", [ident]), ("writer", [])):
            code, out = self.call("know", "list", "--task", task)
            found = [row["id"] for row in out["knowledge"]]
            self.assertEqual((code, found), (0, expected))

    def test_task_refuses_unknown_and_wrong_project_sources(self) -> None:
        """A task cannot override the indexed source's project mapping."""
        code, out = self.call(
            "learn", "request", "--session", "missing", "--task", "parser"
        )
        self.assertEqual((code, out["error"]), (2, "unknown_session"))
        with self.assertRaisesRegex(ValueError, "rejected_scope"):
            self.transaction(
                lambda: review_requests.enqueue_review(
                    self.conn,
                    self.window(),
                    Scope(project_id="other", task="parser"),
                    self.policy,
                    "explicit",
                    15.0,
                )
            )

    def test_task_value_is_bounded_and_project_default_remains(self) -> None:
        """Reject ambiguous IDs and preserve the old project request."""
        for task in ("", "task space", "../task", "a" * 129, "éclair"):
            with self.subTest(task=task):
                code, out = self.request(task)
                self.assertEqual((code, out["error"]), (2, "rejected_scope"))
        source = self.window().source
        code, out = self.call(
            "learn",
            "request",
            "--session",
            f"{source.provider}:{source.thread_id}",
            "--from-line",
            "2",
            "--through-line",
            "3",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["scope"], self.scope.model_dump())
