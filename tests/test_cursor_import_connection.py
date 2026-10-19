"""Cursor import composes with a real caller-owned writer connection."""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from collections.abc import Generator
from pathlib import Path
from unittest import mock

from muninn import cursor_import, erase_collect, store, tombstones
from muninn.tombstone_key import TombstoneKeyError
from tests import test_cursor_precompact

ZERO_COUNTS = {
    "conversations_seen": 0,
    "events_added": 0,
    "events_unchanged": 0,
    "skipped_rows": 0,
    "events_skipped_erased": 0,
}


class CursorConnectionTests(unittest.TestCase):
    """Exercise private fixtures with explicit caller resource lifetimes."""

    def setUp(self) -> None:
        self.fixture = test_cursor_precompact.CursorPrecompactTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.conn.close()
        self.home = self.fixture.home
        self.database = self.fixture.database
        self.cwd = str(self.fixture.repo)

    @contextlib.contextmanager
    def writer(self) -> Generator[sqlite3.Connection]:
        """Open under the actual writer and close before releasing it."""
        with (
            store.writer_lock(self.home, wait_s=0),
            contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn,
        ):
            yield conn

    def thread_intent(self) -> None:
        """Append durable erasure for the active synthetic conversation."""
        row = tombstones.tombstone_row("cursor", "thread", thread_id="active")
        tombstones.append_tombstones(self.home, [row])

    def rows(self, conn: sqlite3.Connection, thread: str) -> list[sqlite3.Row]:
        """Read every event field for exact preservation checks."""
        return list(
            conn.execute(
                "SELECT e.* FROM event e JOIN source s ON s.id=e.source_id"
                " WHERE s.thread_id=? ORDER BY e.seq",
                (thread,),
            )
        )

    def test_import_inside_caller_writer_preserves_connection(self) -> None:
        original = self.database.read_bytes()
        prepared = cursor_import.prepare(self.database, self.cwd)
        with self.writer() as conn:
            with (
                mock.patch.object(
                    store, "writer_lock", side_effect=AssertionError
                ),
                mock.patch.object(
                    store, "connect_rw", side_effect=AssertionError
                ),
                mock.patch("sqlite3.connect", side_effect=AssertionError),
                mock.patch("subprocess.Popen", side_effect=AssertionError),
            ):
                counts = cursor_import.apply(conn, self.home, prepared)
                self.assertEqual(
                    counts,
                    ZERO_COUNTS | {"conversations_seen": 1, "events_added": 2},
                )
                again = cursor_import.apply(conn, self.home, prepared)
            self.assertEqual(
                again, counts | {"events_added": 0, "events_unchanged": 2}
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 2
            )
            self.assertFalse(conn.in_transaction)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("caller writer was released")
        self.assertEqual(self.database.read_bytes(), original)
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_read_finishes_before_actual_writer_contention(self) -> None:
        original = self.database.read_bytes()
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(
                cursor_import, "read", wraps=cursor_import.read
            ) as reading,
        ):
            with self.assertRaises(store.BusyError):
                cursor_import.run(self.home, self.database, self.cwd, 0)
            reading.assert_called_once_with(
                self.database, None, conversation_id=None
            )
            with self.assertRaisesRegex(ValueError, "unsupported_database"):
                cursor_import.run(
                    self.home,
                    self.database.with_suffix(".missing"),
                    self.cwd,
                    0,
                )
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse((self.home / "tombstone.key").exists())

    def test_selected_empty_avoids_writer_open_and_replay(self) -> None:
        cursor_import.run(self.home, self.database, self.cwd, 0)
        with self.writer() as conn:
            before = list(conn.iterdump())
            self.thread_intent()
            with mock.patch.object(
                store, "connect_rw", side_effect=AssertionError
            ):
                counts = cursor_import.run(
                    self.home,
                    self.database,
                    self.cwd,
                    0,
                    max_bubbles=1,
                    conversation_id="active",
                )
                prepared = cursor_import.prepare(
                    self.database,
                    self.cwd,
                    max_bubbles=1,
                    conversation_id="active",
                )
                self.assertEqual(
                    cursor_import.apply(conn, self.home, prepared), counts
                )
            self.assertEqual(
                counts,
                ZERO_COUNTS | {"skipped_rows": 1},
            )
            self.assertEqual(list(conn.iterdump()), before)

    def test_full_empty_replays_intent_and_creates_key(self) -> None:
        with (
            contextlib.closing(sqlite3.connect(self.database)) as source,
            source,
        ):
            source.execute("DELETE FROM cursorDiskKV")
        with store.writer_lock(self.home, wait_s=0):
            self.thread_intent()
        counts = cursor_import.run(self.home, self.database, self.cwd, 0)
        self.assertEqual(
            counts,
            ZERO_COUNTS,
        )
        self.assertEqual(len((self.home / "tombstone.key").read_bytes()), 32)
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            self.assertEqual(
                conn.execute("SELECT thread_id FROM tombstone").fetchone()[0],
                "active",
            )

    def test_exact_refresh_keeps_foreign_rows_and_erased_counts(self) -> None:
        self.fixture.add_conversation("foreign")
        cursor_import.run(self.home, self.database, self.cwd, 0)
        changed = self.fixture.tmp / "new-project"
        changed.mkdir()
        prepared = cursor_import.prepare(
            self.database, str(changed), conversation_id="active"
        )
        original = self.database.read_bytes()
        with self.writer() as conn:
            foreign = self.rows(conn, "foreign")
            erased = self.rows(conn, "active")[0]
            target = erase_collect.Target()
            erase_collect.collect_event(
                conn, target, f"cursor:active:{erased['line']}.1"
            )
            tombstones.append_tombstones(
                self.home,
                target.tombstones
                + erase_collect.content_tombstones(conn, target),
            )
            counts = cursor_import.apply(conn, self.home, prepared)
            self.assertEqual(
                counts,
                ZERO_COUNTS
                | {
                    "conversations_seen": 1,
                    "events_added": 1,
                    "events_skipped_erased": 1,
                },
            )
            rows = self.rows(conn, "active")
            self.assertEqual(
                [(row["cwd"], row["text"]) for row in rows],
                [(str(changed), "active two")],
            )
            self.assertEqual(self.rows(conn, "foreign"), foreign)
            self.assertEqual(
                cursor_import.apply(conn, self.home, prepared),
                counts | {"events_added": 0, "events_unchanged": 1},
            )
        self.assertEqual(self.database.read_bytes(), original)

    def test_thread_tombstone_and_malformed_rows_keep_exact_counts(
        self,
    ) -> None:
        self.fixture.add_conversation("foreign")
        with (
            contextlib.closing(sqlite3.connect(self.database)) as source,
            source,
        ):
            source.execute(
                "INSERT INTO cursorDiskKV"
                " VALUES ('composerData:broken', 'not JSON')"
            )
        prepared = cursor_import.prepare(self.database, self.cwd)
        with self.writer() as conn:
            self.thread_intent()
            counts = cursor_import.apply(conn, self.home, prepared)
            self.assertEqual(
                counts,
                ZERO_COUNTS
                | {
                    "conversations_seen": 2,
                    "events_added": 2,
                    "skipped_rows": 1,
                    "events_skipped_erased": 2,
                },
            )
            self.assertEqual(
                [
                    row[0]
                    for row in conn.execute("SELECT thread_id FROM source")
                ],
                ["foreign"],
            )

    def test_wrong_home_and_memory_refuse_before_replay_or_key(self) -> None:
        prepared = cursor_import.prepare(self.database, self.cwd)
        other = self.fixture.tmp / "other-home"
        with (
            self.writer() as conn,
            contextlib.closing(
                sqlite3.connect(":memory:", isolation_level=None)
            ) as memory,
        ):
            before = list(conn.iterdump())
            for supplied, home in ((conn, other), (memory, self.home)):
                with (
                    self.subTest(home=home),
                    mock.patch.object(
                        tombstones,
                        "reapply_tombstones",
                        side_effect=AssertionError,
                    ),
                    mock.patch.object(
                        cursor_import, "key_for", side_effect=AssertionError
                    ),
                    self.assertRaisesRegex(
                        ValueError, "cursor_import_store_mismatch"
                    ),
                ):
                    cursor_import.apply(supplied, home, prepared)
                self.assertFalse(supplied.in_transaction)
                self.assertEqual(supplied.execute("SELECT 1").fetchone()[0], 1)
            self.assertEqual(list(conn.iterdump()), before)
            self.assertFalse((self.home / "tombstone.key").exists())
            self.assertFalse(other.exists())

    def test_active_transaction_and_implicit_mode_refuse_without_changes(
        self,
    ) -> None:
        prepared = cursor_import.prepare(self.database, self.cwd)
        with self.writer() as conn:
            for active in (False, True):
                conn.isolation_level = None if active else "DEFERRED"
                if active:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO scope(key,label,kind)"
                        " VALUES ('caller','caller','dir')"
                    )
                before = list(conn.iterdump())
                with (
                    self.subTest(active=active),
                    mock.patch.object(
                        tombstones,
                        "reapply_tombstones",
                        side_effect=AssertionError,
                    ),
                    mock.patch.object(
                        cursor_import, "key_for", side_effect=AssertionError
                    ),
                    self.assertRaisesRegex(
                        ValueError, "cursor_import_autocommit_required"
                    ),
                ):
                    cursor_import.apply(conn, self.home, prepared)
                self.assertEqual(conn.in_transaction, active)
                self.assertEqual(list(conn.iterdump()), before)
                if active:
                    conn.execute("ROLLBACK")
            self.assertFalse((self.home / "tombstone.key").exists())

    def test_later_conversation_failure_keeps_prior_commit_and_caller(
        self,
    ) -> None:
        self.fixture.add_conversation("later")
        prepared = cursor_import.prepare(self.database, self.cwd)
        original = self.database.read_bytes()
        with self.writer() as conn:
            conn.execute(
                "CREATE TEMP TRIGGER fail_later BEFORE INSERT ON event"
                " WHEN NEW.text='later two'"
                " BEGIN SELECT RAISE(ABORT, 'cursor fixture failure'); END"
            )
            with self.assertRaises(sqlite3.IntegrityError) as raised:
                cursor_import.apply(conn, self.home, prepared)
            self.assertEqual(
                raised.exception.args, ("cursor fixture failure",)
            )
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                [
                    row[0]
                    for row in conn.execute("SELECT thread_id FROM source")
                ],
                ["active"],
            )
            self.assertEqual(
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT text FROM event ORDER BY seq"
                    )
                ],
                ["active one", "active two"],
            )
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("caller writer was released after error")
        with self.writer() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 2
            )
        self.assertEqual(self.database.read_bytes(), original)

    def test_replay_and_key_errors_keep_original_exception_and_connection(
        self,
    ) -> None:
        prepared = cursor_import.prepare(self.database, self.cwd)
        error = OSError("replay fixture failure")
        with self.writer() as conn:
            with mock.patch.object(
                tombstones, "reapply_tombstones", side_effect=error
            ):
                with self.assertRaises(OSError) as raised:
                    cursor_import.apply(conn, self.home, prepared)
                self.assertIs(raised.exception, error)
            key = self.home / "tombstone.key"
            key.write_bytes(b"broken")
            key.chmod(0o600)
            with self.assertRaisesRegex(TombstoneKeyError, "damaged"):
                cursor_import.apply(conn, self.home, prepared)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM source").fetchone()[0], 0
            )
            self.assertEqual(key.read_bytes(), b"broken")
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_wrapper_closes_its_connection_and_releases_writer_on_error(
        self,
    ) -> None:
        original_connect = store.connect_rw
        connections: list[sqlite3.Connection] = []
        error = OSError("replay fixture failure")

        def connect(path: Path) -> sqlite3.Connection:
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("wrapper opened outside writer")
            conn = original_connect(path)
            connections.append(conn)
            return conn

        with (
            mock.patch.object(store, "connect_rw", side_effect=connect),
            mock.patch.object(
                tombstones, "reapply_tombstones", side_effect=error
            ),
        ):
            with self.assertRaises(OSError) as raised:
                cursor_import.run(self.home, self.database, self.cwd, 0)
            self.assertIs(raised.exception, error)
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        with store.writer_lock(self.home, wait_s=0):
            pass
