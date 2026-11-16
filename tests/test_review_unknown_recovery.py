"""Exercise explicit uncertainty acceptance in disposable governed stores."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from contextlib import closing
from unittest import mock

from install.context import StepFailedError
from install.learning_policy_recovery import require_finished
from muninn import (
    cli_parser,
    review_commit,
    review_unknown_recovery,
)
from muninn import review_pause as pause
from muninn.knowledge_models import ReviewResult
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_runtime_models import ManagedCallRecord
from muninn.review_supervisor_store import halt_interrupted
from muninn.review_unknown_state import UnknownAcknowledgement
from tests.unknown_recovery_support import UnknownRecoveryCase


@unittest.skipIf(sys.platform == "win32", "requires POSIX")
class UnknownRecoveryTests(UnknownRecoveryCase):
    """Preserve failed history while fencing one explicitly named unknown."""

    def test_preserves_history_and_accepts_checked_legacy_bridge(self) -> None:
        conn = self.fixture.conn
        before = {
            table: [
                tuple(row) for row in conn.execute(f"SELECT * FROM {table}")
            ]
            for table in ("review_call", "review_attempt", "review_request")
        }
        self.assertNotEqual(
            before["review_call"][0][3],
            conn.execute(
                "SELECT policy_digest FROM review_request"
            ).fetchone()[0],
        )
        generation = conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        result = self.recover()
        self.assertEqual(result.resulting_generation, generation + 1)
        self.assertEqual(
            tuple(
                conn.execute(
                    "SELECT admission,generation FROM review_control"
                ).fetchone()
            ),
            (1, generation + 1),
        )
        for table, rows in before.items():
            self.assertEqual(
                [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")],
                rows,
            )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM knowledge").fetchone()[0], 0
        )

    def test_repeat_after_later_stop_does_not_reopen(self) -> None:
        first = self.recover()
        self.fixture.conn.execute(
            "UPDATE review_control SET admission=0,generation=generation+1"
        )
        stopped = tuple(
            self.fixture.conn.execute(
                "SELECT * FROM review_control"
            ).fetchone()
        )
        self.assertEqual(self.recover(), first)
        self.assertEqual(
            tuple(
                self.fixture.conn.execute(
                    "SELECT * FROM review_control"
                ).fetchone()
            ),
            stopped,
        )

    def test_failed_bridge_rolls_back_generation_admission_and_ack(
        self,
    ) -> None:
        before = tuple(
            self.fixture.conn.execute(
                "SELECT * FROM review_control"
            ).fetchone()
        )
        with (
            mock.patch.object(
                review_unknown_recovery,
                "effective_policy",
                side_effect=ValueError("review_policy_required"),
            ),
            self.assertRaisesRegex(ValueError, "recovery_refused"),
        ):
            self.recover()
        self.assertEqual(
            tuple(
                self.fixture.conn.execute(
                    "SELECT * FROM review_control"
                ).fetchone()
            ),
            before,
        )

    def test_acknowledged_call_allows_installer_and_supervisor_guards(
        self,
    ) -> None:
        self.recover()
        require_finished(self.fixture.conn)
        self.fixture.transaction(
            lambda: halt_interrupted(self.fixture.conn, self.fixture.policy)
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )

    def test_cli_accepts_only_an_explicit_named_acknowledgement(self) -> None:
        args = cli_parser.build_parser().parse_args(
            [
                "learn",
                "recover-unknown",
                "--attempt",
                self.context.attempt_id,
                "--round",
                "1",
                "--accept-uncertainty",
            ]
        )
        self.assertEqual(args.learn_cmd, "recover-unknown")
        self.assertEqual(args.attempt_id, self.context.attempt_id)

    def test_changed_call_is_not_exempt_after_acknowledgement(self) -> None:
        self.recover()
        self.fixture.conn.execute("UPDATE review_call SET outcome='changed'")
        with self.assertRaisesRegex(
            StepFailedError, "learning_recovery_unverified"
        ):
            require_finished(self.fixture.conn)
        with self.assertRaisesRegex(ValueError, "recovery_refused"):
            self.recover()

    def test_recovery_never_publishes_admission_before_ack_commit(
        self,
    ) -> None:
        before = tuple(
            self.fixture.conn.execute(
                "SELECT * FROM review_control"
            ).fetchone()
        )
        save = review_unknown_recovery.state.save

        def inspect(
            conn: sqlite3.Connection, acknowledgement: UnknownAcknowledgement
        ) -> None:
            self.assertEqual(
                tuple(
                    self.fixture.conn.execute(
                        "SELECT * FROM review_control"
                    ).fetchone()
                ),
                before,
            )
            save(conn, acknowledgement)

        with mock.patch.object(
            review_unknown_recovery.state, "save", side_effect=inspect
        ):
            self.recover()

    def test_recorded_managed_launch_cannot_be_acknowledged(self) -> None:
        raw = self.fixture.conn.execute(
            "SELECT requested_json FROM review_call"
        ).fetchone()[0]
        record = ManagedCallRecord.model_validate_json(raw)
        launched = record.model_copy(
            update={"launched_identity": record.candidates[0].digest}
        )
        self.fixture.conn.execute(
            "UPDATE review_call SET requested_json=?",
            (canonical_json(launched),),
        )
        with self.assertRaisesRegex(ValueError, "recovery_refused"):
            self.recover()

    def test_requires_explicit_uncertainty_acceptance(self) -> None:
        with self.assertRaisesRegex(ValueError, "recovery_refused"):
            review_unknown_recovery.recover(
                self.fixture.home,
                self.context.attempt_id,
                1,
                accept_uncertainty=False,
                now=20.0,
            )

    def test_refuses_recorded_launch_or_unfinished_lineage(self) -> None:
        updates = (
            ("review_call", "state", "launched"),
            ("review_call", "launched_at", 14.0),
            ("review_call", "process_identity_json", "{}"),
            ("review_attempt", "ended_at", None),
            ("review_attempt", "outcome", "abandoned"),
            ("review_request", "state", "waiting"),
        )
        for table, column, value in updates:
            before = self.fixture.conn.execute(
                f"SELECT {column} FROM {table}"
            ).fetchone()[0]
            with self.subTest(table=table, column=column):
                self.fixture.conn.execute(
                    f"UPDATE {table} SET {column}=?", (value,)
                )
                with self.assertRaisesRegex(ValueError, "recovery_refused"):
                    self.recover()
                self.fixture.conn.execute(
                    f"UPDATE {table} SET {column}=?", (before,)
                )

    def test_new_unknown_call_keeps_restart_closed(self) -> None:
        self.recover()
        conn = self.fixture.conn
        row = dict(conn.execute("SELECT * FROM review_call").fetchone())
        row["round_index"] = 2
        conn.execute(
            "INSERT INTO review_call VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
        with self.assertRaisesRegex(
            StepFailedError, "learning_recovery_unverified"
        ):
            require_finished(conn)
        self.fixture.transaction(
            lambda: halt_interrupted(conn, self.fixture.policy)
        )
        self.assertEqual(
            conn.execute("SELECT admission FROM review_control").fetchone()[0],
            0,
        )

    def test_existing_legacy_call_and_request_bridge_to_current_policy(
        self,
    ) -> None:
        conn = self.fixture.conn
        old = ReviewPolicy.model_validate_json(
            conn.execute("SELECT policy_json FROM review_request").fetchone()[
                0
            ]
        )
        conn.execute(
            "UPDATE review_call SET policy_digest=?,requested_json=?",
            (self.fixture.digest(old), canonical_json(old.runner)),
        )
        result = self.recover()
        self.assertEqual(
            result.call_policy_digest, result.request_policy_digest
        )
        self.assertNotEqual(result.policy_digest, result.call_policy_digest)

    def test_existing_legacy_v3_runner_remains_supported(self) -> None:
        conn = self.fixture.conn
        old = ReviewPolicy.model_validate_json(
            conn.execute("SELECT policy_json FROM review_request").fetchone()[
                0
            ]
        )
        conn.execute(
            "UPDATE review_call SET policy_digest=?,requested_json=?",
            (self.fixture.digest(old), canonical_json(old.runner)),
        )
        conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(old), self.fixture.digest(old)),
        )
        self.publish_legacy_authority(old)
        assert old.experimental_codex is not None
        with mock.patch(
            "muninn.review_codex_policy.host_identity",
            return_value=old.experimental_codex.host_identity,
        ):
            result = self.recover()
        self.assertEqual(result.policy_digest, result.request_policy_digest)

    def test_failed_maintenance_verification_changes_nothing(self) -> None:
        before = tuple(
            self.fixture.conn.execute(
                "SELECT * FROM review_control"
            ).fetchone()
        )
        with (
            mock.patch.object(
                pause,
                "verify_current",
                side_effect=ValueError("pause_unverified"),
            ),
            self.assertRaisesRegex(ValueError, "recovery_refused"),
        ):
            self.recover()
        self.assertEqual(
            tuple(
                self.fixture.conn.execute(
                    "SELECT * FROM review_control"
                ).fetchone()
            ),
            before,
        )

    def test_late_old_result_is_still_rejected(self) -> None:
        self.recover()
        context = review_commit.CommitContext(
            home=self.fixture.home,
            request_id=self.context.request_id,
            attempt_id=self.context.attempt_id,
            generation=self.context.generation,
            evidence_ids=(),
            now=21.0,
        )
        result = ReviewResult(
            schema_version=1,
            changes=[],
            unresolved=[],
            no_change_reason="No supported change",
        )
        with self.assertRaisesRegex(
            review_commit.CommitRejectedError, "rejected_fenced"
        ):
            review_commit.commit_review(self.fixture.conn, context, result)

    def test_absent_auxiliary_table_never_grants_exemption(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            self.assertIsNone(
                review_unknown_recovery.state.read(
                    conn, self.context.attempt_id, 1
                )
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE name='review_unknown_ack'"
                ).fetchone()
            )

    def test_post_verification_call_change_refuses_without_ack(self) -> None:
        verify = pause.verify_current

        def changed(
            owner: pause.MaintenanceOwner, conn: sqlite3.Connection
        ) -> None:
            verify(owner, conn)
            conn.execute("UPDATE review_call SET outcome='changed'")

        with (
            mock.patch.object(pause, "verify_current", side_effect=changed),
            self.assertRaisesRegex(ValueError, "recovery_refused"),
        ):
            self.recover()
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM review_unknown_ack"
            ).fetchone()[0],
            0,
        )

    def test_legacy_recovery_requires_its_existing_capability(self) -> None:
        conn = self.fixture.conn
        old = ReviewPolicy.model_validate_json(
            conn.execute("SELECT policy_json FROM review_request").fetchone()[
                0
            ]
        )
        conn.execute(
            "UPDATE review_call SET policy_digest=?,requested_json=?",
            (self.fixture.digest(old), canonical_json(old.runner)),
        )
        conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(old), self.fixture.digest(old)),
        )
        assert old.experimental_codex is not None
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value=old.experimental_codex.host_identity,
            ),
            self.assertRaises((ValueError, OSError)),
        ):
            self.recover()
        self.assertEqual(
            conn.execute("SELECT admission FROM review_control").fetchone()[0],
            0,
        )
