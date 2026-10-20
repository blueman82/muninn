"""Check explicit recovery after a contained Codex authority refusal."""

from __future__ import annotations

import unittest
from uuid import uuid4

from muninn import cli_learn, cli_parser, review_requests
from muninn.review_json import canonical_json
from tests.test_review_codex_production import experimental_policy
from tests.test_review_requests import RequestCase


class ResumeReviewTests(RequestCase):
    """Keep stopped admission closed except for the inspected failure."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = experimental_policy(self.tmp)
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "rejected_authority", 13.0
            )
        )
        self.conn.execute("UPDATE review_control SET admission=0")
        self.request_id = request.id

    def test_exact_contained_refusal_can_resume(self) -> None:
        path = self.tmp / "approved-policy.json"
        path.write_text(canonical_json(self.policy))
        args = cli_parser.build_parser().parse_args(
            [
                "learn",
                "resume",
                "--policy",
                str(path),
                "--failed-request",
                self.request_id,
            ]
        )
        self.assertEqual(
            cli_learn._write(self.conn, args),
            {"admission": True, "policy_id": self.policy.policy_id},
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM review_request WHERE id=?",
                (self.request_id,),
            ).fetchone()[0],
            "failed",
        )

    def test_other_failure_or_unfinished_call_stays_stopped(self) -> None:
        self.conn.execute(
            "UPDATE review_attempt SET error_code='rejected_validation' "
            "WHERE request_id=?",
            (self.request_id,),
        )
        with self.assertRaisesRegex(ValueError, "recovery_refused"):
            self.transaction(
                lambda: review_requests.resume_admission(
                    self.conn, self.policy, self.request_id, 14.0
                )
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )

    def test_later_abandoned_attempt_cannot_reuse_old_refusal(self) -> None:
        other = f"R_{uuid4().hex}"
        self.conn.execute(
            "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,trigger,origin,state,created_at) "
            "SELECT ?,?,provider,thread_id,session_root,3,through_line,"
            "boundary_hash,scope_json,policy_json,policy_digest,trigger,"
            "origin,'failed',14.0 FROM review_request WHERE id=?",
            (other, uuid4().hex, self.request_id),
        )
        self.conn.execute(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "requested_json,observed_json,started_at,progress_at,ended_at,"
            "outcome,error_code,usage_json) "
            "SELECT ?,request_id,generation+1,requested_json,observed_json,"
            "14.0,15.0,15.0,'abandoned',NULL,usage_json "
            "FROM review_attempt WHERE request_id=?",
            (f"A_{uuid4().hex}", self.request_id),
        )
        self.conn.execute(
            "UPDATE review_attempt SET request_id=? "
            "WHERE started_at=14.0 AND outcome='abandoned'",
            (other,),
        )
        with self.assertRaisesRegex(ValueError, "recovery_refused"):
            self.transaction(
                lambda: review_requests.resume_admission(
                    self.conn, self.policy, self.request_id, 16.0
                )
            )


if __name__ == "__main__":
    unittest.main()
