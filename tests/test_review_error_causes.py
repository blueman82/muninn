"""Keep launch errors content-free and the old config sentence gone."""

from __future__ import annotations

import contextlib
import io
import os
import time
from pathlib import Path
from unittest import TestCase, mock

import pydantic

from muninn import cli, hook_status, review_recovery
from muninn import review_supervisor as supervision
from muninn.review_recovery_codes import EXPLANATIONS
from tests.test_review_supervisor import SupervisorCase

SECRET_MARKER = "SECRET-MARKER-9f3a"
OLD = ("could not verify the review setup", "specific cause was not recorded")
ROOT = Path(__file__).resolve().parents[1] / "muninn"


class _Model(pydantic.BaseModel):
    """Carry a value that makes validation fail with attacker text."""

    value: int


def _validation_error() -> pydantic.ValidationError:
    """Build an error whose text holds the marker."""
    try:
        _Model.model_validate({"value": SECRET_MARKER})
    except pydantic.ValidationError as error:
        return error
    raise AssertionError("validation unexpectedly passed")


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
        self.check(_validation_error())

    def test_os_error_text_is_never_stored(self) -> None:
        self.check(OSError(SECRET_MARKER))


class OldSentenceTests(TestCase):
    """Make the old unverifiable-setup notice unreachable."""

    def test_no_explanation_contains_the_old_sentences(self) -> None:
        for code in (*EXPLANATIONS, "config", "unknown_code"):
            for stage in ("launch", "not recorded"):
                text = " ".join(review_recovery.explain(code, stage).values())
                for old in OLD:
                    self.assertNotIn(old, text.lower(), code)

    def test_source_has_no_old_sentence(self) -> None:
        for path in ROOT.rglob("*.py"):
            text = " ".join(path.read_text().lower().split())
            for old in OLD:
                self.assertNotIn(old, text, str(path))
