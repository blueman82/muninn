"""Dismiss saved request notices without changing durable learning history."""

from __future__ import annotations

import sqlite3
from functools import partial
from unittest import mock

from muninn import hook, review_recovery, review_requests
from tests.ingest_support import TID
from tests.test_cli_learning import CliLearningCase


class LearningNoticeDismissalTests(CliLearningCase):
    """Use the real CLI and disposable evidence to isolate notice deletion."""

    def failed_request(self) -> str:
        """Persist a failed attempt and its separate safe notice."""
        ident = self.context.request_id
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, self.context.attempt_id, "rejected_authority", 20.0
            )
        )
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "rejected_authority", "review", 20.0
            )
        )
        return ident

    def test_dismiss_preserves_history_and_diagnostics(self) -> None:
        ident = self.failed_request()
        self.conn.execute("UPDATE review_control SET admission=0")
        code, before = self.call("learn", "show", ident)
        self.assertEqual(code, 0)
        control = tuple(self.row("SELECT * FROM review_control"))
        code, out = self.call("learn", "dismiss", ident)
        self.assertEqual(
            (code, out["request_id"], out["dismissed"]), (0, ident, True)
        )
        self.assertIsNone(review_recovery.notice(self.conn))
        code, after = self.call("learn", "show", ident)
        self.assertEqual(code, 0)
        for field in ("request", "attempts", "calls", "evidence", "receipt"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["request"]["state"], "failed")
        self.assertEqual(after["diagnostics"][0]["code"], "rejected_authority")
        self.assertEqual(after["diagnostics"][0]["stage"], "not recorded")
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_control")), control
        )
        code, listed = self.call("learn", "list", "--state", "failed")
        self.assertEqual((code, listed["requests"][0]["id"]), (0, ident))
        code, out = self.call("learn", "dismiss", ident)
        self.assertEqual((code, out["dismissed"]), (0, False))

    def test_dismiss_targets_one_request_and_keeps_setup_notice(self) -> None:
        ident = self.failed_request()
        other = self.enqueue(self.window(2, 2))
        for request_id, code in ((other.id, "config"), ("setup", "auth")):
            self.transaction(
                partial(
                    review_recovery.record,
                    self.conn,
                    request_id,
                    code,
                    "launch",
                    30.0,
                )
            )
        self.assertEqual(self.call("learn", "dismiss", ident)[0], 0)
        rows = self.conn.execute("SELECT id FROM review_issue ORDER BY id")
        self.assertEqual([row[0] for row in rows], sorted((other.id, "setup")))
        self.assertEqual(self.call("learn", "dismiss", "setup")[0], 2)
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], "setup")

    def test_dismiss_rejects_unknown_or_invalid_requests(self) -> None:
        ident = self.failed_request()
        code, out = self.call("learn", "dismiss", "R_" + "a" * 32)
        self.assertEqual((code, out["error"]), (2, "request_unavailable"))
        self.assertEqual(self.call("learn", "dismiss", "R1")[0], 2)
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], ident)

    def test_new_failure_after_dismissal_shows_again(self) -> None:
        ident = self.failed_request()
        self.assertEqual(self.call("learn", "dismiss", ident)[0], 0)
        self.transaction(
            lambda: review_recovery.record(
                self.conn, ident, "rejected_scope", "review", 40.0
            )
        )
        issue = review_recovery.notice(self.conn)
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(
            (issue["id"], issue["code"]), (ident, "rejected_scope")
        )

    def test_failed_dismissal_rolls_back_only_notice_delete(self) -> None:
        ident = self.failed_request()
        clear = review_recovery.clear

        def interrupted(conn: sqlite3.Connection, request_id: str) -> bool:
            clear(conn, request_id)
            raise ValueError("interrupted")

        with mock.patch.object(review_recovery, "clear", interrupted):
            code, out = self.call("learn", "dismiss", ident)
        self.assertEqual((code, out["error"]), (2, "interrupted"))
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], ident)

    def test_dismiss_without_optional_notice_table_is_a_noop(self) -> None:
        self.conn.execute("DROP TABLE review_issue")
        code, out = self.call("learn", "dismiss", self.context.request_id)
        self.assertEqual((code, out["dismissed"]), (0, False))

    def test_dismissed_notice_leaves_the_next_session_hook(self) -> None:
        ident = self.failed_request()
        payload = {"session_id": TID, "cwd": str(self.cwd)}
        env = {"MUNINN_HOME": str(self.home)}
        for mode in ("recall.off", "recall.advisory"):
            marker = self.home / mode
            marker.touch()
            before = hook.session_start(payload, "codex", env)
            self.assertIn(ident, str(before.get("systemMessage")))
            marker.unlink()
        self.assertEqual(self.call("learn", "dismiss", ident)[0], 0)
        after = hook.session_start(payload, "codex", env)
        self.assertNotIn(ident, str(after.get("systemMessage")))
        self.assertNotIn("Muninn learning", str(after.get("systemMessage")))
