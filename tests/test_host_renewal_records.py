"""Strict persisted host-renewal phase and approval contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import policy_records, record
from install.context import Ctx, StepFailedError
from install.host_renewal_models import RECEIPT, HostApproval
from install.host_renewal_preview import preview, scan_state
from muninn.review_json import canonical_json
from muninn.review_progress import digest
from tests.host_renewal_record_support import ReceiptFixture
from tests.policy_preview_support import no_command, states
from tools import prepare_codex_policy


class HostRenewalRecordTests(ReceiptFixture):
    """Reject ambiguous authority and require each durable predecessor."""

    def test_first_activation_without_renewal_is_unchanged(self) -> None:
        original = {
            "policy_activation": {
                "phase": "intent",
                "intent": {
                    "store": [1, 2],
                    "control": preview(
                        self.ctx, self.repo, self.sha
                    ).state.control,
                    "boundaries": "a" * 64,
                },
            }
        }
        self.assertEqual(policy_records.parse(json.dumps(original)), original)

    def test_every_phase_requires_its_predecessor(self) -> None:
        self.ctx.rdir.mkdir(mode=0o700)
        for phase in (
            "approved_intent",
            "contained_archived",
            "commit_pending",
            "grant_committed",
            "restart_pending",
            "upgrade_verified",
        ):
            with self.subTest(phase=phase):
                value = self.phase(phase)
                typed = policy_records.host_renewal(value)
                envelope = {
                    "host_renewal": RECEIPT.dump_python(typed, mode="json")
                }
                record.save(self.ctx, envelope)
                loaded = policy_records.parse(
                    (self.ctx.rdir / "rollback-record.json").read_bytes()
                )
                self.assertEqual(
                    policy_records.host_renewal(loaded["host_renewal"]), typed
                )
                required = ["plan", "approval", "operation_identity"]
                if phase != "approved_intent":
                    required += [
                        "contained",
                        "archive",
                        "owner_control",
                        "containment_finished_at",
                    ]
                if phase not in ("approved_intent", "contained_archived"):
                    required += ["predecessor", "successor", "ready"]
                for key in required:
                    broken = dict(value)
                    del broken[key]
                    with (
                        self.subTest(missing=key),
                        self.assertRaises(StepFailedError),
                    ):
                        policy_records.host_renewal(broken)

    def test_scalar_coercion_and_ambiguous_json_are_refused(self) -> None:
        value = self.initial()
        for key, replacement in (
            ("version", True),
            ("version", 1.0),
            ("phase", "unknown"),
            ("sha", "malformed"),
            ("unexpected", "extra"),
        ):
            with (
                self.subTest(key=key, replacement=replacement),
                self.assertRaises(StepFailedError),
            ):
                policy_records.host_renewal(value | {key: replacement})
        for key, replacement in (
            ("confirmed", 1),
            ("approved_at", 10),
            ("approved_at", "10.0"),
            ("approved_at", float("inf")),
            ("plan_digest", "b" * 64),
        ):
            broken = value | {
                "approval": value["approval"] | {key: replacement}
            }
            with (
                self.subTest(key=key, replacement=replacement),
                self.assertRaises(StepFailedError),
            ):
                policy_records.host_renewal(broken)
        with self.assertRaises(StepFailedError):
            policy_records.parse('{"host_renewal":{},"host_renewal":{}}')

    def test_full_envelope_bound_refuses_before_publication(self) -> None:
        self.ctx.rdir.mkdir(mode=0o700)
        record.save(self.ctx, {"host_renewal": self.initial()})
        before = states(self.home)
        for phase in ("approved_intent", "commit_pending"):
            with self.subTest(phase=phase):
                value = self.phase(phase)
                oversized = {
                    "host_renewal": value,
                    "snapshot": "x" * (4 * 1024 * 1024),
                }
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_limit"
                ):
                    record.save(self.ctx, oversized)
                self.assertEqual(states(self.home), before)

    def test_receipt_selectors_and_self_digest_are_exact(self) -> None:
        value = self.initial()
        for key, replacement in (
            ("home", str(self.home / "other")),
            ("repo", str(self.repo / "other")),
            ("sha", "b" * 40),
            ("operation", "../other"),
        ):
            with self.subTest(key=key), self.assertRaises(StepFailedError):
                policy_records.host_renewal(value | {key: replacement})
        for key in ("digest", "bindings_digest", "artifacts_digest"):
            broken = value | {"plan": value["plan"] | {key: "0" * 64}}
            with self.subTest(key=key), self.assertRaises(StepFailedError):
                policy_records.host_renewal(broken)
        with self.assertRaises(StepFailedError):
            policy_records.host_renewal(
                value | {"approval": value["approval"] | {"confirmed": False}}
            )

    def test_verification_never_turns_unknown_or_failed_into_success(
        self,
    ) -> None:
        value = self.phase("upgrade_verified")
        value["verification"].insert(
            0, {"check": "codex_hook_hashes_match", "ok": None}
        )
        typed = policy_records.host_renewal(value)
        assert typed.phase == "upgrade_verified"
        self.assertIsNone(typed.verification[0].ok)
        for replacement in (1, False, "true"):
            broken = json.loads(json.dumps(value))
            broken["verification"][-1]["ok"] = replacement
            with (
                self.subTest(replacement=replacement),
                self.assertRaises(StepFailedError),
            ):
                policy_records.host_renewal(broken)
        for replacement in (
            value["verification"] * 2,
            list(reversed(value["verification"])),
        ):
            with self.assertRaises(StepFailedError):
                policy_records.host_renewal(
                    value | {"verification": replacement}
                )

    def test_pending_requires_exact_control_successor(self) -> None:
        value = self.phase("commit_pending")
        for key, replacement in (
            ("generation", value["predecessor"]["generation"]),
            ("admission", 1 - value["predecessor"]["admission"]),
            ("policy_digest", "f" * 64),
            ("activated_at", 999.0),
        ):
            broken = value | {
                "successor": value["successor"] | {key: replacement}
            }
            with self.subTest(key=key), self.assertRaises(StepFailedError):
                policy_records.host_renewal(broken)
        broken = value | {
            "predecessor": value["predecessor"] | {"generation": 999}
        }
        with self.assertRaises(StepFailedError):
            policy_records.host_renewal(broken)

    def test_contained_control_and_preserved_progress_are_exact(self) -> None:
        value = self.phase("contained_archived")
        for key, replacement in (
            ("progress_digest", "f" * 64),
            ("erasure_digest", "f" * 64),
            ("store_identity", [9, 9]),
        ):
            with self.subTest(key=key), self.assertRaises(StepFailedError):
                policy_records.host_renewal(
                    value
                    | {"contained": value["contained"] | {key: replacement}}
                )
        with self.assertRaises(StepFailedError):
            policy_records.host_renewal(
                value
                | {
                    "owner_control": value["owner_control"]
                    | {"generation": 999}
                }
            )

    def test_named_projection_rejects_receipts_and_substitutions(self) -> None:
        request = self.request()
        attempt = self.attempt(request)
        self.insert(
            "review_receipt",
            {
                "request_id": request,
                "attempt_id": attempt,
                "applied_json": "[]",
                "unresolved_json": "[]",
                "committed_at": 3.0,
                "outcome": "no_change",
            },
        )
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.sql("DELETE FROM review_receipt")
        self.sql(
            "UPDATE review_request SET current_attempt_id=?",
            ("A_" + "2" * 32,),
        )
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_each_job_uses_its_own_stored_retry_limits(self) -> None:
        first = self.request()
        self.attempt(first)
        third, _ = prepare_codex_policy.policy(
            self.ctx.data, self.executable, "owner", 1.0, revision=3
        )
        second, attempt = "R_" + "2" * 32, "A_" + "2" * 32
        self.sql(
            "INSERT INTO review_request SELECT ?,?,provider,'second',"
            "session_root,"
            "from_line,through_line,boundary_hash,scope_json,?,?,trigger,"
            "origin,"
            "'running',?,created_at,retry_at,cancel_code,candidate_id,"
            "decision_revision "
            "FROM review_request WHERE id=?",
            (
                second,
                "second",
                canonical_json(third),
                digest(third),
                attempt,
                first,
            ),
        )
        self.sql(
            "INSERT INTO review_attempt SELECT ?,?,generation,requested_json,"
            "observed_json,started_at,progress_at,ended_at,outcome,error_code,"
            "usage_json "
            "FROM review_attempt WHERE id=?",
            (attempt, second, "A_" + "1" * 32),
        )
        plan = preview(self.ctx, self.repo, self.sha)
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
            "outcome='abandoned'"
        )
        self.sql(
            "UPDATE review_request SET state='failed',current_attempt_id=NULL"
        )
        self.sql(
            "UPDATE review_request SET state='waiting',retry_at=8.0 "
            "WHERE id=?",
            (second,),
        )
        with closing(sqlite3.connect(self.path)) as conn:
            checked = scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=plan.containment,
            )
        self.assertEqual(
            [job.request.state for job in checked.containment.jobs],
            ["failed", "waiting"],
        )
        self.sql(
            "UPDATE review_request SET retry_at=9.0 WHERE id=?", (second,)
        )
        with (
            closing(sqlite3.connect(self.path)) as conn,
            self.assertRaises(StepFailedError),
        ):
            scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=plan.containment,
            )
        self.sql(
            "UPDATE review_request SET retry_at=8.0 WHERE id=?", (second,)
        )
        self.sql(
            "INSERT INTO review_attempt SELECT ?,request_id,generation,"
            "requested_json,observed_json,started_at,progress_at,ended_at,"
            "outcome,error_code,usage_json FROM review_attempt WHERE id=?",
            ("A_" + "3" * 32, attempt),
        )
        with (
            closing(sqlite3.connect(self.path)) as conn,
            self.assertRaises(StepFailedError),
        ):
            scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=plan.containment,
            )

    def test_duplicate_or_unsorted_plan_ids_refuse_with_valid_digest(
        self,
    ) -> None:
        value = self.initial()
        for key, ids in (
            ("request_ids", ["R_" + "1" * 32] * 2),
            ("attempt_ids", ["A_" + "1" * 32] * 2),
            ("request_ids", ["R_" + "2" * 32, "R_" + "1" * 32]),
        ):
            plan = value["plan"] | {key: ids}
            fields = {
                name: item for name, item in plan.items() if name != "digest"
            }
            plan["digest"] = hashlib.sha256(
                json.dumps(
                    fields,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            broken = value | {
                "plan": plan,
                "approval": value["approval"]
                | {"plan_digest": plan["digest"]},
            }
            with (
                self.subTest(key=key, ids=ids),
                self.assertRaises(StepFailedError),
            ):
                policy_records.host_renewal(broken)


class HostRenewalPortableTests(unittest.TestCase):
    """Check portable consent and unsupported-platform refusal."""

    def test_unsupported_platform_refuses_before_artifact_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(mode=0o700)
            ctx = Ctx(
                home=home,
                run=no_command,
                ts="unsupported",
                upgrade=True,
                dry_run=True,
                platform="win32",
                review_policy=home / "candidate.json",
            )
            before = states(home)
            with (
                mock.patch(
                    "tools.prepare_codex_policy.prepare",
                    side_effect=AssertionError("prepare"),
                ),
                mock.patch(
                    "muninn.review_pause.maintenance_owner",
                    side_effect=AssertionError("owner"),
                ),
                mock.patch(
                    "muninn.review_codex_launch.verify_codex_executable",
                    side_effect=AssertionError("process"),
                ),
                self.assertRaisesRegex(
                    StepFailedError, "host_renewal_unsupported"
                ),
            ):
                preview(ctx, home / "repo", "a" * 40)
            self.assertEqual(states(home), before)

    def test_approval_requires_exact_affirmative_data(self) -> None:
        data = {
            "plan_digest": "a" * 64,
            "approved_at": 1.0,
            "authority": "installer_prompt",
            "confirmed": True,
        }
        self.assertTrue(HostApproval.model_validate(data).confirmed)
        for key, value in (
            ("confirmed", 1),
            ("confirmed", False),
            ("approved_at", 1),
            ("approved_at", "1.0"),
        ):
            with self.subTest((key, value)), self.assertRaises(ValueError):
                HostApproval.model_validate(data | {key: value})
