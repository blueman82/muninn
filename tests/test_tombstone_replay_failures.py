"""Transactional, cleanup-retry and physical privacy replay obligations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest import mock

from muninn import knowledge_erase, store, tombstones
from muninn import review_pause as q
from muninn import review_pause_state as state
from tests.test_review_commit import CommitCase
from tests.test_review_pause import PauseCase
from tests.tombstone_replay_support import CANARY, TARGET, ReplayCase


class TombstoneFailureTests(ReplayCase):
    """Refusal never commits a partial plan or steals caller ownership."""

    def prepare(self) -> None:
        """Generate real source rows and durable session intent."""
        self.session(TARGET, CANARY)
        self.run_ingest()
        tombstones.append_tombstones(
            self.home,
            [
                tombstones.tombstone_row(
                    "codex", "session", session_root=TARGET
                )
            ],
        )

    def test_active_transaction_rejected_before_intent_read(self) -> None:
        self.prepare()
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE source SET status='missing'")
        with (
            mock.patch.object(
                tombstones, "read_intent", side_effect=AssertionError
            ),
            self.assertRaisesRegex(ValueError, "^replay_autocommit_required$"),
        ):
            self.replay()
        self.assertTrue(self.conn.in_transaction)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("tombstone"), 0)

    def test_mutation_failure_rolls_back_every_key_and_target(self) -> None:
        self.prepare()
        self.conn.execute(
            "CREATE TRIGGER refuse_delete BEFORE DELETE ON source "
            "BEGIN SELECT RAISE(ABORT,'synthetic_delete_failure'); END"
        )
        before = list(self.conn.iterdump())
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "synthetic_delete_failure"
        ):
            self.replay()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(
            [
                s
                for s in statements
                if s in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")
            ],
            ["BEGIN IMMEDIATE", "ROLLBACK"],
        )

    def test_rollback_failure_does_not_mask_original_sql_error(self) -> None:
        self.prepare()
        self.conn.execute(
            "CREATE TRIGGER refuse_delete BEFORE DELETE ON source "
            "BEGIN SELECT RAISE(ABORT,'original_delete_error'); END"
        )

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK"
                else sqlite3.SQLITE_OK
            )

        self.conn.set_authorizer(authorize)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "^original_delete_error$"
            ):
                self.replay()
        finally:
            self.conn.set_authorizer(None)
            self.conn.execute("ROLLBACK")
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("tombstone"), 0)

    def test_commit_failure_rolls_back_before_cleanup(self) -> None:
        self.prepare()
        before = list(self.conn.iterdump())

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
                else sqlite3.SQLITE_OK
            )

        self.conn.set_authorizer(authorize)
        with (
            mock.patch.object(
                tombstones,
                "erase_attempt_material",
                side_effect=AssertionError,
            ),
            self.assertRaises(sqlite3.DatabaseError),
        ):
            self.replay()
        self.conn.set_authorizer(None)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_single_transaction_without_new_connections_roots_or_processes(
        self,
    ) -> None:
        self.prepare()
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        with store.writer_lock(self.home, wait_s=0):
            with (
                mock.patch.object(
                    sqlite3, "connect", side_effect=AssertionError
                ),
                mock.patch.object(
                    store, "writer_lock", side_effect=AssertionError
                ),
                mock.patch("subprocess.Popen", side_effect=AssertionError),
            ):
                self.assertEqual(
                    tombstones.reapply_tombstones(self.conn, self.home), 1
                )
                tombstones.require_current_tombstones(self.conn, self.home)
            self.assertEqual(self.conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(
            [
                s
                for s in statements
                if s in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")
            ],
            ["BEGIN IMMEDIATE", "COMMIT"],
        )

    def test_committed_transcript_scrub_retries_busy_wal_with_zero_keys(
        self,
    ) -> None:
        self.prepare()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=0")
        journal = (self.home / "tombstones.jsonl").read_bytes()
        with closing(
            sqlite3.connect(store.db_path(self.home), isolation_level=None)
        ) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM event").fetchall()
            with self.assertRaisesRegex(OSError, "^erasure_checkpoint_busy$"):
                self.replay()
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(
                (
                    self.count("event"),
                    self.count("source"),
                    self.count("tombstone"),
                ),
                (0, 0, 1),
            )
            self.assertTrue(
                any(
                    CANARY.encode() in p.read_bytes()
                    for p in (
                        store.db_path(self.home),
                        Path(str(store.db_path(self.home)) + "-wal"),
                    )
                    if p.exists()
                )
            )
        self.assertEqual(self.replay(), 0)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(store.db_path(self.home)) + suffix)
            if path.exists():
                self.assertNotIn(CANARY.encode(), path.read_bytes())
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM event_fts WHERE event_fts MATCH ?",
                (CANARY,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            (self.home / "tombstones.jsonl").read_bytes(), journal
        )


class TombstoneCleanupTests(CommitCase):
    """Committed support identities retain retryable working-file cleanup."""

    def setUp(self) -> None:
        super().setUp()
        self.work = self.home / "review-work" / self.context.attempt_id
        self.work.mkdir(parents=True)
        (self.work / "prompt.txt").write_text("synthetic governed material")
        tombstones.append_tombstones(
            self.home,
            [
                tombstones.tombstone_row(
                    "codex", "thread", thread_id=self.evidence[0].thread_id
                )
            ],
        )
        self.conn.close()

    def test_filesystem_failure_after_commit_is_retryable_with_zero_keys(
        self,
    ) -> None:
        original = PermissionError("synthetic cleanup failed")
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(store.db_path(self.home))) as conn,
        ):
            with (
                mock.patch.object(
                    knowledge_erase.os, "unlink", side_effect=original
                ),
                self.assertRaises(PermissionError) as caught,
            ):
                tombstones.reapply_tombstones(conn, self.home)
            self.assertIs(caught.exception, original)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM review_evidence LIMIT 1"
                ).fetchone()[0],
                "erased",
            )
            self.assertTrue(self.work.is_dir())
            self.assertEqual(tombstones.reapply_tombstones(conn, self.home), 0)
            self.assertFalse(self.work.exists())

    def test_auxiliary_query_failure_closes_connection_and_retry_cleans(
        self,
    ) -> None:
        opened: list[sqlite3.Connection] = []
        real = store.connect_ro

        def readonly(path: Path) -> sqlite3.Connection:
            self.assertEqual(path, store.db_path(self.home))
            conn = real(path)
            opened.append(conn)
            conn.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
            return conn

        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(store.db_path(self.home))) as conn,
        ):
            with (
                mock.patch.object(store, "connect_ro", side_effect=readonly),
                self.assertRaises(sqlite3.DatabaseError),
            ):
                tombstones.reapply_tombstones(conn, self.home)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertTrue(self.work.exists())
            self.assertEqual(tombstones.reapply_tombstones(conn, self.home), 0)
            self.assertFalse(self.work.exists())

    def test_auxiliary_open_failure_keeps_caller_open_and_retry_cleans(
        self,
    ) -> None:
        original = OSError("synthetic cleanup open failure")
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(store.db_path(self.home))) as conn,
        ):
            with (
                mock.patch.object(store, "connect_ro", side_effect=original),
                self.assertRaises(OSError) as caught,
            ):
                tombstones.reapply_tombstones(conn, self.home)
            self.assertIs(caught.exception, original)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertTrue(self.work.exists())
            self.assertEqual(tombstones.reapply_tombstones(conn, self.home), 0)
            self.assertFalse(self.work.exists())


class ReplayConnectionLifetimeTests(PauseCase):
    """Keep replay and caller connection inside both actual barriers."""

    def test_erase_opens_and_closes_connection_inside_both_barriers(
        self,
    ) -> None:
        connect = store.connect_rw
        events: list[str] = []

        def barriers() -> None:
            with self.assertRaises(BlockingIOError):
                state.acquire(self.home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("writer not held")

        def checked(path: Path) -> sqlite3.Connection:
            barriers()
            events.append("open")
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def close() -> None:
                barriers()
                conn.close()
                events.append("close")

            proxy.close.side_effect = close
            return cast(sqlite3.Connection, proxy)

        with (
            mock.patch.object(store, "connect_rw", side_effect=checked),
            q.erase_connection(self.home, wait_s=0, status=self.status),
        ):
            events.append("caller")
        self.assertEqual(events, ["open", "caller", "close"])
