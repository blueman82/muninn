"""Keep launch errors content-free and the old config sentence gone."""

from __future__ import annotations

import contextlib
import io
import os
import time
from unittest import mock

from muninn import cli, hook_status
from muninn import review_supervisor as supervision
from tests.secret_support import SECRET_MARKER, validation_error
from tests.test_review_supervisor import SupervisorCase


class LaunchErrorPrivacyTests(SupervisorCase):
    """Raise marker-bearing errors in the launch path."""

    def run_failing(self, error: Exception) -> None:
        """Fail every launch preparation with `error` until terminal."""
        with mock.patch.object(
            supervision, "prepare_launch", side_effect=error
        ):
            until = time.monotonic() + 8
            while time.monotonic() < until:
                self.supervisor.tick(13.0)
                row = self.conn.execute(
                    "SELECT state FROM review_request WHERE id=?",
                    (self.request.id,),
                ).fetchone()
                if row[0] == "failed":
                    return
                time.sleep(0.01)
        self.fail("request did not fail")

    def surfaces(self) -> str:
        """Collect database, hook, CLI and call-log text."""
        dump = "\n".join(self.conn.iterdump())
        parts = [dump]
        parts.extend(
            hook_status.learning_status({"code": row[0]})
            for row in self.conn.execute("SELECT code FROM review_issue")
        )
        out = io.StringIO()
        env = {"MUNINN_HOME": str(self.home)}
        with (
            mock.patch.dict(os.environ, env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            cli.main(["learn", "show", self.request.id])
        parts.append(out.getvalue())
        calls = self.home / "calls.jsonl"
        if calls.exists():
            parts.append(calls.read_text())
        return "\n".join(parts)

    def check(self, error: Exception) -> None:
        """Assert the code is the fallback and the marker is absent."""
        self.run_failing(error)
        code = self.conn.execute(
            "SELECT error_code FROM review_attempt"
        ).fetchone()
        self.assertEqual(code[0], "config")
        self.assertNotIn(SECRET_MARKER, self.surfaces())

    def test_validation_error_text_is_never_stored(self) -> None:
        self.check(validation_error())

    def test_os_error_text_is_never_stored(self) -> None:
        self.check(OSError(SECRET_MARKER))
