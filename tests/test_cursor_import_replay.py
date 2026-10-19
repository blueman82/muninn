"""Governed replay cleanup keeps its bounded read-only connection lifetime."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

from muninn import cursor_import, store, tombstones
from tests import test_cursor_precompact, test_review_commit
from tests.ingest_support import TID
from tests.test_cursor_import_connection import ZERO_COUNTS


class CursorReplayTests(unittest.TestCase):
    """Exercise actual admitted attempts, durable erasure and working files."""

    def setUp(self) -> None:
        self.governed = test_review_commit.CommitCase()
        self.addCleanup(self.governed.doCleanups)
        self.governed.setUp()
        self.cursor = test_cursor_precompact.CursorPrecompactTests()
        self.addCleanup(self.cursor.doCleanups)
        self.cursor.setUp()
        self.home = self.governed.home
        self.db = store.db_path(self.home)
        self.work = (
            self.home / "review-work" / self.governed.context.attempt_id
        )
        self.work.mkdir(parents=True)
        self.result = self.work / "result.json"
        self.result.write_text('{"synthetic": true}\n')
        self.prompt = self.work / "prompt.txt"
        self.prompt.write_text("synthetic governed prompt")
        self.original = self.governed.path.read_bytes()
        self.governed.conn.close()
        self.cursor.conn.close()
        self.opened: list[sqlite3.Connection] = []
        self.paths: list[Path] = []
        self.uris: list[str] = []
        self.queries: list[str] = []
        self.denied: list[str | None] = []
        self.open_error = sqlite3.OperationalError("fixture open failure")

    def prepare(self, *, empty: bool = False) -> cursor_import.PreparedImport:
        """Read the actual Cursor fixture before entering the writer."""
        if empty:
            with closing(sqlite3.connect(self.cursor.database)) as conn, conn:
                conn.execute("DELETE FROM cursorDiskKV")
        self.cursor_original = self.cursor.database.read_bytes()
        return cursor_import.prepare(
            self.cursor.database, str(self.cursor.repo)
        )

    @contextmanager
    def writer(self) -> Generator[sqlite3.Connection]:
        """Open the current caller connection under its actual writer."""
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(self.db)) as conn,
        ):
            row = tombstones.tombstone_row("codex", "thread", thread_id=TID)
            tombstones.append_tombstones(self.home, [row])
            yield conn

    def assert_scrubbed(self, conn: sqlite3.Connection) -> None:
        """Require committed logical scrub without claiming file cleanup."""
        self.assertFalse(conn.in_transaction)
        self.assertEqual(
            tuple(
                conn.execute(
                    "SELECT state,current_attempt_id,cancel_code"
                    " FROM review_request"
                ).fetchone()
            ),
            ("cancelled", None, "evidence_erased"),
        )
        self.assertEqual(
            conn.execute("SELECT outcome FROM review_attempt").fetchone()[0],
            "evidence_erased",
        )
        self.assertEqual(
            [
                tuple(row)
                for row in conn.execute(
                    "SELECT state,excerpt FROM review_evidence"
                )
            ],
            [("erased", None)] * len(self.governed.evidence),
        )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
        )
        self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    @contextmanager
    def observe(
        self, caller: sqlite3.Connection, *, fault: str = ""
    ) -> Generator[None]:
        """Observe the real same-store RO lookup and inject SQLite faults."""
        real_connect, real_ro = sqlite3.connect, store.connect_ro

        def connect(
            database: str, *, uri: bool, isolation_level: None
        ) -> sqlite3.Connection:
            self.assertEqual(
                database, self.db.absolute().as_uri() + "?mode=ro"
            )
            self.assertTrue(uri)
            self.assertIsNone(isolation_level)
            self.uris.append(database)
            if fault == "open":
                raise self.open_error
            return real_connect(
                database, uri=uri, isolation_level=isolation_level
            )

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table == "review_attempt":
                self.denied.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def readonly(path: Path) -> sqlite3.Connection:
            self.assertEqual(path, self.db)
            self.assert_scrubbed(caller)
            self.paths.append(path)
            conn = real_ro(path)
            self.opened.append(conn)
            conn.set_trace_callback(self.queries.append)
            if fault == "query":
                conn.set_authorizer(authorize)
            return conn

        with (
            mock.patch.object(store, "connect_ro", side_effect=readonly),
            mock.patch("sqlite3.connect", side_effect=connect),
            mock.patch.object(store, "connect_rw", side_effect=AssertionError),
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError
            ),
            mock.patch.object(
                cursor_import, "read", side_effect=AssertionError
            ),
            mock.patch("subprocess.Popen", side_effect=AssertionError),
        ):
            yield

    def assert_resources(
        self, conn: sqlite3.Connection, *, opened: int
    ) -> None:
        """Check auxiliary opens, closure and retained caller ownership."""
        self.assertEqual(self.paths, [self.db])
        self.assertEqual(self.uris, [self.db.absolute().as_uri() + "?mode=ro"])
        self.assertEqual(len(self.opened), opened)
        for extra in self.opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                extra.execute("SELECT 1")
        self.assert_scrubbed(conn)
        with (
            self.assertRaises(store.BusyError),
            store.writer_lock(self.home, wait_s=0),
        ):
            self.fail("caller writer released")
        self.assertEqual(
            self.cursor.database.read_bytes(), self.cursor_original
        )
        self.assertEqual(self.governed.path.read_bytes(), self.original)

    def assert_released(self) -> None:
        """Reopen after release and independently confirm durable scrub."""
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(self.db)) as conn,
        ):
            self.assert_scrubbed(conn)

    def test_governed_cleanup_allows_only_same_store_ro_authentication(
        self,
    ) -> None:
        prepared = self.prepare()
        with self.writer() as conn:
            with self.observe(conn):
                counts = cursor_import.apply(conn, self.home, prepared)
            self.assert_resources(conn, opened=1)
            self.assertEqual(
                self.queries,
                [
                    "SELECT 1 FROM review_attempt WHERE id="
                    f"'{self.governed.context.attempt_id}'"
                ],
            )
            self.assertFalse(self.work.exists())
            self.assertEqual(
                counts,
                ZERO_COUNTS | {"conversations_seen": 1, "events_added": 2},
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM event e"
                    " JOIN source s ON s.id=e.source_id"
                    " WHERE s.provider='cursor'"
                ).fetchone()[0],
                2,
            )
        self.assert_released()

    def test_full_empty_import_still_authenticates_governed_cleanup(
        self,
    ) -> None:
        prepared = self.prepare(empty=True)
        with self.writer() as conn:
            with self.observe(conn):
                counts = cursor_import.apply(conn, self.home, prepared)
            self.assert_resources(conn, opened=1)
            self.assertEqual(counts, ZERO_COUNTS)
            self.assertFalse(self.work.exists())
        self.assert_released()

    def assert_failed_cleanup(self, conn: sqlite3.Connection) -> None:
        """Require retained work files and no Cursor writes after failure."""
        self.assertEqual(self.result.read_text(), '{"synthetic": true}\n')
        self.assertEqual(self.prompt.read_text(), "synthetic governed prompt")
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM source WHERE provider='cursor'"
            ).fetchone()[0],
            0,
        )

    def test_auxiliary_query_failure_closes_handle_after_committed_scrub(
        self,
    ) -> None:
        prepared = self.prepare()
        with self.writer() as conn:
            with (
                self.observe(conn, fault="query"),
                mock.patch.object(
                    cursor_import, "key_for", side_effect=AssertionError
                ),
                self.assertRaises(sqlite3.DatabaseError) as raised,
            ):
                cursor_import.apply(conn, self.home, prepared)
            self.assertIn("prohibited", str(raised.exception))
            self.assertEqual(self.denied, ["review_attempt"])
            self.assert_resources(conn, opened=1)
            self.assert_failed_cleanup(conn)
        self.assert_released()

    def test_auxiliary_open_failure_preserves_scrub_and_prevents_cursor_writes(
        self,
    ) -> None:
        prepared = self.prepare()
        with self.writer() as conn:
            with (
                self.observe(conn, fault="open"),
                mock.patch.object(
                    cursor_import, "key_for", side_effect=AssertionError
                ),
                self.assertRaises(store.StoreUnavailableError) as raised,
            ):
                cursor_import.apply(conn, self.home, prepared)
            self.assertIs(raised.exception.__cause__, self.open_error)
            self.assert_resources(conn, opened=0)
            self.assert_failed_cleanup(conn)
        self.assert_released()
