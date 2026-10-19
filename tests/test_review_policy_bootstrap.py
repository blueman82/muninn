"""First policy activation stays inside the caller's atomic writer scope."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from unittest import mock

from muninn import review_progress, review_quiescence, store
from muninn.review_models import ReviewPolicy
from tests.store_support import (
    StoreCase,
    insert_event,
    insert_scope,
    insert_source,
)
from tests.test_review_shared_models import policy_data


class PolicyBootstrapTests(StoreCase):
    """Use real disposable state and a caller-held physical writer lock."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(store.writer_lock(self.home, wait_s=0))
        self.conn = self.rw()
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.policy = ReviewPolicy.model_validate(data)
        self.encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        self.expected_digest = hashlib.sha256(
            self.encoded.encode()
        ).hexdigest()
        scope = insert_scope(self.conn)
        insert_source(self.conn)
        self.conn.execute(
            "UPDATE source SET cursor_line=17,anchor_sha256='indexed'"
        )
        cursor = insert_source(self.conn, "cursor-thread")
        self.conn.execute(
            "UPDATE source SET provider='cursor',root='cursor-imports',"
            "parse_state='{}',cursor_line=999,anchor_sha256='wrong' "
            "WHERE id=?",
            (cursor,),
        )
        insert_event(self.conn, cursor, scope, line=4)
        insert_event(self.conn, cursor, scope, line=9)
        self.conn.execute("UPDATE review_control SET generation=7")

    def bootstrap(self, *, now: float = 20.0) -> None:
        """Require the public interface before checking activation effects."""
        self.assertTrue(hasattr(review_progress, "bootstrap_policy"))
        review_progress.bootstrap_policy(self.conn, self.policy, now)

    def state(self) -> tuple[list[tuple[object, ...]], ...]:
        """Read the observable control, baseline and request rows."""
        return tuple(
            [tuple(row) for row in self.conn.execute(f"SELECT * FROM {name}")]
            for name in ("review_control", "review_source", "review_request")
        )

    def test_exact_snapshot_and_policy_are_uncommitted_until_caller_commit(
        self,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        with (
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError
            ),
            mock.patch.object(
                review_quiescence,
                "maintenance_owner",
                side_effect=AssertionError,
            ),
            mock.patch("sqlite3.connect", side_effect=AssertionError),
            mock.patch("subprocess.Popen", side_effect=AssertionError),
        ):
            self.bootstrap()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(
            self.state(),
            (
                [(1, 7, 1, self.encoded, self.expected_digest, 20.0)],
                [
                    ("codex", "t1", 17, 17, 17, 17, "indexed", 20.0),
                    ("cursor", "cursor-thread", 9, 9, 9, 9, "h", 20.0),
                ],
                [],
            ),
        )
        self.conn.execute("COMMIT")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT 1").fetchone()[0], 1)

    def test_caller_rollback_restores_never_activated_state(self) -> None:
        before = self.state()
        self.conn.execute("BEGIN IMMEDIATE")
        self.bootstrap()
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.state(), before)
        self.conn.execute("BEGIN IMMEDIATE")
        self.bootstrap()
        self.conn.execute("COMMIT")
        self.assertEqual(self.state()[0][0][2], 1)

    def test_missing_transaction_and_invalid_times_leave_state_unchanged(
        self,
    ) -> None:
        before = self.state()
        with self.assertRaisesRegex(ValueError, "caller_transaction_required"):
            self.bootstrap()
        self.conn.execute("BEGIN IMMEDIATE")
        for value in (-1.0, float("nan"), float("inf"), 0, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bootstrap(now=value)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.state(), before)
        self.conn.execute("ROLLBACK")

    def test_unchecked_policy_copies_are_revalidated_without_changes(
        self,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        before, original = self.state(), self.policy
        for fields in (
            {"max_rounds": 0},
            {"max_attempts": True},
            {"projection_version": "unknown"},
        ):
            self.policy = original.model_copy(update=fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.bootstrap()
            self.assertEqual(self.state(), before)
        self.conn.execute("ROLLBACK")

    def test_repeated_and_configured_disabled_activation_are_refused(
        self,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        self.bootstrap()
        self.conn.execute("COMMIT")
        for admission in (1, 0):
            self.conn.execute(
                "UPDATE review_control SET admission=?", (admission,)
            )
            before = self.state()
            self.conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "review_bootstrap_state"):
                self.bootstrap()
            self.assertEqual(self.state(), before)
            self.conn.execute("COMMIT")

    def test_partial_control_and_old_source_baselines_are_refused(
        self,
    ) -> None:
        for sql in (
            "UPDATE review_control SET admission=1",
            "UPDATE review_control SET policy_json='{}'",
            "UPDATE review_control SET policy_digest='old'",
            "UPDATE review_control SET activated_at=0",
            "DELETE FROM review_control",
            "INSERT INTO review_source VALUES('codex','old',0,0,0,0,NULL,0)",
            "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,trigger,origin,state,created_at) "
            "VALUES('R_old','old','codex','old','old',1,1,'h','{}','{}','h',"
            "'owner','review','pending',0)",
        ):
            with self.subTest(sql=sql):
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(sql)
                before = self.state()
                with self.assertRaisesRegex(
                    ValueError, "review_bootstrap_state"
                ):
                    self.bootstrap()
                self.assertEqual(self.state(), before)
                self.conn.execute("ROLLBACK")

    def test_bad_source_or_control_write_does_not_leak_partial_activation(
        self,
    ) -> None:
        for sql, error in (
            (
                "UPDATE source SET parse_state=NULL WHERE provider='cursor'",
                ValueError,
            ),
            (
                "CREATE TEMP TRIGGER refuse_activation BEFORE UPDATE ON "
                "review_control BEGIN SELECT RAISE("
                "ABORT,'activation failed'); END",
                sqlite3.IntegrityError,
            ),
        ):
            with self.subTest(sql=sql):
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(sql)
                before = self.state()
                with self.assertRaises(error):
                    self.bootstrap()
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.state(), before)
                self.conn.execute("COMMIT")
                self.conn.execute(
                    "DROP TRIGGER IF EXISTS temp.refuse_activation"
                )
                self.conn.execute("UPDATE source SET parse_state='{}'")

    def test_unmarked_store_is_refused(self) -> None:
        self.conn.execute("PRAGMA user_version=3")
        self.conn.execute("BEGIN IMMEDIATE")
        before = self.state()
        with self.assertRaisesRegex(
            store.StoreUnavailableError, "cutover_required"
        ):
            self.bootstrap()
        self.assertEqual(self.state(), before)
        self.conn.execute("ROLLBACK")
