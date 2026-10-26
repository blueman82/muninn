"""Separate stored learning admission from configuration and past errors."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from unittest import mock

from pydantic import ValidationError

from muninn import obs, obs_stats, review_recovery, review_requests
from muninn.review_models import ReviewAdmission
from muninn.review_policy_status import policy_status
from tests.test_review_requests import RequestCase

SECRET = "private-admission-policy-canary"


class AdmissionTests(RequestCase):
    """Observe private disposable control state without changing it."""

    def checks(self) -> dict[str, obs.CheckResult]:
        """Collect finite health results from the existing snapshot reader."""
        return {c["check"]: c for c in obs.review_checks(self.conn, 20.0)}

    def update(self, **fields: object) -> None:
        """Set only test-authored control columns in a disposable store."""
        columns = ",".join(name + "=?" for name in fields)
        self.conn.execute(
            "UPDATE review_control SET " + columns, tuple(fields.values())
        )

    def test_closed_admission_is_explicit_despite_configured_policy(
        self,
    ) -> None:
        self.conn.execute("UPDATE review_control SET admission=0")
        before = self.conn.total_changes
        checks = {c["check"]: c for c in obs.review_checks(self.conn, 20.0)}
        self.assertEqual(checks["review_policy"]["detail"], "configured")
        self.assertTrue(checks["review_policy"]["ok"])
        self.assertIn("review_admission", checks)
        self.assertIs(checks["review_admission"]["ok"], False)
        self.assertEqual(checks["review_admission"]["detail"], "closed")
        self.assertEqual(checks["review_admission"]["level"], "info")
        report = obs_stats.review_stats(self.conn, 20.0).model_dump(
            mode="json"
        )
        self.assertEqual(
            report["admission"], {"enabled": False, "policy_status": "valid"}
        )
        self.assertEqual(self.conn.total_changes, before)

    def test_open_valid_control_reports_only_stored_prerequisites(
        self,
    ) -> None:
        before = self.conn.total_changes
        with (
            mock.patch(
                "subprocess.Popen", side_effect=AssertionError("spawn")
            ),
            mock.patch(
                "muninn.store.writer_lock",
                side_effect=AssertionError("writer"),
            ),
        ):
            check = self.checks()["review_admission"]
            report = obs_stats.review_stats(self.conn, 20.0)
        self.assertEqual(
            (check["ok"], check["detail"], check["level"]),
            (True, "open", "info"),
        )
        self.assertEqual(
            report.admission,
            ReviewAdmission(enabled=True, policy_status="valid"),
        )
        self.assertEqual(report.requests_by_state, {})
        self.assertEqual(report.calls_by_state, {})
        self.assertEqual(report.completed_records, 0)
        self.assertEqual(self.conn.total_changes, before)

    def test_missing_policy_keeps_closed_control_informational(self) -> None:
        self.update(admission=0, policy_json=None, policy_digest=None)
        check = self.checks()["review_admission"]
        self.assertEqual(
            (check["ok"], check["detail"], check["level"]),
            (False, "closed_policy_absent", "info"),
        )
        self.assertEqual(
            policy_status(self.conn),
            ReviewAdmission(enabled=False, policy_status="policy_absent"),
        )

    def test_enabled_control_without_policy_warns(self) -> None:
        self.update(policy_json=None, policy_digest=None)
        check = self.checks()["review_admission"]
        self.assertEqual(
            (check["ok"], check["detail"], check["level"]),
            (False, "policy_absent", "warn"),
        )

    def test_invalid_policy_never_exposes_contents_or_errors(self) -> None:
        for switch, expected, level in (
            (0, "closed_policy_invalid", "info"),
            (1, "policy_invalid", "warn"),
        ):
            with self.subTest(switch=switch):
                self.update(
                    admission=switch, policy_json=json.dumps({SECRET: SECRET})
                )
                checks = self.checks()
                check = checks["review_admission"]
                report = obs_stats.review_stats(self.conn, 20.0)
                self.assertEqual(
                    (check["ok"], check["detail"], check["level"]),
                    (False, expected, level),
                )
                self.assertEqual(
                    report.admission.policy_status, "policy_invalid"
                )
                self.assertNotIn(SECRET, json.dumps(checks))
                self.assertNotIn(SECRET, report.model_dump_json())

    def test_wrong_projection_is_distinct_from_configured_policy(self) -> None:
        data = self.policy.model_dump(mode="json")
        data["projection_version"] = "permitted-fields-v1"
        self.update(policy_json=json.dumps(data))
        checks = self.checks()
        self.assertEqual(checks["review_policy"]["detail"], "configured")
        self.assertEqual(
            checks["review_admission"]["detail"], "policy_invalid"
        )
        self.assertFalse(checks["review_admission"]["ok"])

    def test_digest_mismatch_is_explicit_for_both_switch_states(self) -> None:
        for switch, expected, level in (
            (0, "closed_policy_digest_mismatch", "info"),
            (1, "policy_digest_mismatch", "warn"),
        ):
            with self.subTest(switch=switch):
                self.update(admission=switch, policy_digest="0" * 64)
                check = self.checks()["review_admission"]
                self.assertEqual(
                    (check["ok"], check["detail"], check["level"]),
                    (False, expected, level),
                )
                self.assertEqual(
                    policy_status(self.conn).policy_status,
                    "policy_digest_mismatch",
                )

    def test_raw_json_order_and_whitespace_do_not_change_integrity(
        self,
    ) -> None:
        data = self.policy.model_dump(mode="json")
        reordered = dict(reversed(tuple(data.items())))
        self.update(policy_json=json.dumps(reordered, indent=4))
        self.assertEqual(policy_status(self.conn).policy_status, "valid")
        self.assertEqual(self.checks()["review_admission"]["detail"], "open")

    def test_missing_singleton_is_explicit_without_generation_assumptions(
        self,
    ) -> None:
        self.conn.execute("DELETE FROM review_control")
        before = self.conn.total_changes
        checks = self.checks()
        self.assertEqual(checks["review_policy"]["detail"], "control_absent")
        self.assertIsNone(checks["review_admission"]["ok"])
        self.assertEqual(
            checks["review_admission"]["detail"], "control_absent"
        )
        report = obs_stats.review_stats(self.conn, 20.0)
        self.assertEqual(
            report.admission,
            ReviewAdmission(enabled=None, policy_status="control_absent"),
        )
        self.assertEqual(self.conn.total_changes, before)

    def test_missing_control_retains_independent_historical_diagnostics(
        self,
    ) -> None:
        past = self.enqueue(self.window(2, 2))
        ended = self.transaction(
            lambda: review_requests.claim_attempt(self.conn, past.id, 1, 12.0)
        )
        self.conn.execute(
            "UPDATE review_attempt SET ended_at=13.0,outcome='failed',"
            "error_code='config' WHERE id=?",
            (ended.id,),
        )
        self.conn.execute(
            "UPDATE review_request SET state='failed' WHERE id=?", (past.id,)
        )
        running = self.enqueue(self.window(3, 3))
        self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, running.id, 1, 14.0
            )
        )
        self.enqueue()
        self.transaction(
            lambda: review_recovery.record(
                self.conn, "setup", "config", "policy", 15.0
            )
        )
        before = self.checks()
        self.conn.execute("DELETE FROM review_control")
        changes = self.conn.total_changes
        after = self.checks()
        self.assertEqual(set(after), set(before))
        for name in (
            "review_config",
            "review_auth",
            "review_process_deadline",
            "review_commit_delay",
            "review_cancel_delay",
            "review_backlog",
            "review_coverage_gaps",
            "review_attention",
        ):
            with self.subTest(check=name):
                self.assertEqual(after[name], before[name])
        self.assertEqual(after["review_config"]["detail"], "1")
        self.assertEqual(after["review_backlog"]["detail"], "1")
        self.assertEqual(
            after["review_abandoned"],
            {
                "check": "review_abandoned",
                "ok": None,
                "detail": "generation_unavailable",
                "level": "warn",
            },
        )
        self.assertEqual(after["review_admission"]["detail"], "control_absent")
        self.assertEqual(
            after["review_capture_gaps"],
            {
                "check": "review_capture_gaps",
                "ok": None,
                "detail": "control_absent",
                "level": "warn",
            },
        )
        self.assertEqual(self.conn.total_changes, changes)

    def test_damaged_switch_is_unknown_instead_of_truthy(self) -> None:
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.update(admission=2)
        self.conn.execute("PRAGMA ignore_check_constraints=OFF")
        self.assertEqual(
            policy_status(self.conn),
            ReviewAdmission(enabled=None, policy_status="control_invalid"),
        )
        check = self.checks()["review_admission"]
        self.assertIsNone(check["ok"])
        self.assertEqual(check["detail"], "control_invalid")

    def test_read_only_observations_preserve_caller_transaction(self) -> None:
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.execute("BEGIN")
        before = self.conn.total_changes
        try:
            self.assertEqual(policy_status(self.conn).policy_status, "valid")
            self.assertEqual(
                self.checks()["review_admission"]["detail"], "open"
            )
            self.assertTrue(
                obs_stats.review_stats(self.conn, 20.0).admission.enabled
            )
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.total_changes, before)
        finally:
            self.conn.execute("ROLLBACK")
            self.conn.execute("PRAGMA query_only=OFF")

    def test_old_config_error_remains_separate_from_current_admission(
        self,
    ) -> None:
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.conn.execute(
            "UPDATE review_attempt SET ended_at=13.0,outcome='failed',"
            "error_code='config' WHERE id=?",
            (attempt.id,),
        )
        self.conn.execute(
            "UPDATE review_request SET state='failed' WHERE id=?",
            (request.id,),
        )
        self.update(generation=2, admission=0)
        checks = self.checks()
        self.assertEqual(checks["review_config"]["detail"], "1")
        self.assertEqual(checks["review_admission"]["detail"], "closed")
        self.assertEqual(checks["review_admission"]["level"], "info")
        self.assertEqual(policy_status(self.conn).policy_status, "valid")

    def test_unrelated_database_failure_is_not_hidden_as_policy_status(
        self,
    ) -> None:
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            self.assertRaises(sqlite3.OperationalError),
        ):
            policy_status(conn)

    def test_diagnostic_model_rejects_inconsistent_or_untyped_controls(
        self,
    ) -> None:
        for enabled, status in (
            (None, "valid"),
            (True, "control_absent"),
            ("false", "valid"),
            (False, SECRET),
        ):
            with (
                self.subTest(enabled=enabled, status=status),
                self.assertRaises(ValidationError),
            ):
                ReviewAdmission.model_validate(
                    {"enabled": enabled, "policy_status": status}
                )
