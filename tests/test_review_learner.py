"""Check the learner with a fake claude program and a real store."""

from __future__ import annotations

import sys
import time
from unittest import mock

from muninn import review_provider_claude, review_requests, review_settings
from muninn.review_json import canonical_json
from muninn.review_learner import Learner
from tests.test_review_requests import RequestCase

FINAL = (
    "{'op':'final','result':{'schema_version':1,'changes':CHANGES,"
    "'unresolved':[],'no_change_reason':REASON}}"
)
NOTE = (
    "[{'action':'add','form':'note','kind':'fact','scope':d['scope'],"
    "'content':'Keep this fact.','conditions':[],"
    "'evidence_refs':[d['evidence_ids'][0]],'reason':'Supported.'}]"
)


def final(changes: str = "[]") -> str:
    """Return a literal final operation for the fake program."""
    reason = "'No supported changes'" if changes == "[]" else "None"
    return FINAL.replace("CHANGES", changes).replace("REASON", reason)


class LearnerCase(RequestCase):
    """Run Learner ticks against a fake claude that answers from a script."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = review_settings.Settings(enabled=True)
        review_settings.save(self.home, self.settings)
        self.policy = review_settings.policy_for(self.settings).model_copy(
            update={"deadline_seconds": 2.0}
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.request = self.enqueue()
        self.exe = self.home / "fake-claude"
        self.fake(final())
        patch = mock.patch.object(
            review_provider_claude, "find_executable", self.find
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.learner = Learner(self.home)

    def find(self) -> str | None:
        """Stand in for the executable lookup."""
        return str(self.exe) if self.exe.exists() else None

    def fake(self, first: str, second: str = "None") -> None:
        """Write a program that answers round 1 and round 2 from literals."""
        self.exe.write_text(
            f"#!{sys.executable}\n"
            "import json,sys,time\n"
            "p=json.load(sys.stdin); d=p['data']; n=d['round_index']\n"
            f"op=({first}) if n==1 else ({second})\n"
            "sys.stdout.write(op if isinstance(op,str) else json.dumps("
            "{'type':'result','subtype':'success','is_error':False,"
            "'structured_output':op}))\n"
        )
        self.exe.chmod(0o755)

    def attempt(self) -> tuple[str | None, str | None]:
        """Return the latest attempt's error code and its request state."""
        return (
            self.conn.execute(
                "SELECT error_code FROM review_attempt"
            ).fetchone()[0],
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
        )

    def count(self, table: str) -> int:
        """Count the rows of one table."""
        return int(
            self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        )


class LearnerTests(LearnerCase):
    """One tick takes one request through its calls."""

    def test_success_writes_memory(self) -> None:
        self.fake(final(NOTE))
        self.learner.tick(13.0)
        self.assertEqual(self.attempt(), (None, "completed"))
        self.assertEqual(self.count("review_receipt"), 1)
        self.assertEqual(self.count("knowledge"), 1)

    def test_disabled_settings_make_no_call(self) -> None:
        review_settings.save(
            self.home, self.settings.model_copy(update={"enabled": False})
        )
        self.learner.tick(13.0)
        self.assertEqual(self.count("review_attempt"), 0)
        self.assertEqual(self.count("review_call"), 0)

    def test_hang_times_out_and_next_pass_retries(self) -> None:
        self.fake("time.sleep(60)")
        began = time.monotonic()
        self.learner.tick(13.0)
        self.assertLess(time.monotonic() - began, 30)
        self.assertEqual(self.attempt(), ("deadline", "waiting"))
        self.assertEqual(
            self.conn.execute("SELECT outcome FROM review_call").fetchone()[0],
            "deadline",
        )
        self.fake(final())
        self.learner.tick(14.0)
        self.assertEqual(self.count("review_receipt"), 0)
        self.learner.tick(200.0)
        self.assertEqual(self.count("review_receipt"), 1)

    def test_output_that_is_not_json_is_rejected(self) -> None:
        self.fake("'not json'")
        self.learner.tick(13.0)
        self.assertEqual(self.attempt()[0], "rejected_validation")

    def test_missing_program_is_unavailable(self) -> None:
        self.exe.unlink()
        self.learner.tick(13.0)
        self.assertEqual(self.attempt()[0], "unavailable")

    def test_bad_format_gets_one_correction_round(self) -> None:
        self.fake("{'op':'final','raw':'x'}", final())
        self.learner.tick(13.0)
        self.assertEqual(self.attempt(), (None, "completed"))
        self.assertEqual(
            [
                row[0]
                for row in self.conn.execute(
                    "SELECT outcome FROM review_call ORDER BY round_index"
                )
            ],
            ["correction_format_requested", "completed"],
        )

    def test_second_format_failure_has_no_third_call(self) -> None:
        self.fake("{'op':'final','raw':'x'}", "{'op':'final','raw':'x'}")
        self.learner.tick(13.0)
        self.assertEqual(self.attempt()[0], "rejected_validation")
        self.assertEqual(self.count("review_call"), 2)

    def test_one_read_round_then_final(self) -> None:
        self.fake("{'op':'search','query':'x','scope':d['scope']}", final())
        self.learner.tick(13.0)
        self.assertEqual(self.attempt(), (None, "completed"))
        self.assertEqual(self.count("review_call"), 2)

    def test_running_attempt_of_a_stopped_process_is_interrupted(self) -> None:
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, self.request.id, 1, 12.0
            )
        )
        self.exe.unlink()
        self.learner.tick(13.0)
        row = self.conn.execute(
            "SELECT outcome FROM review_attempt WHERE id=?", (attempt.id,)
        ).fetchone()
        self.assertEqual(row[0], "abandoned")
