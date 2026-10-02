"""Query contract: unreadable stores and read-only access."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from muninn import query, store
from tests.query_support import OpenCase, QueryCase, open_specs
from tests.test_store import SPILLING_WRITER, Child


class StoreTroubleTests(QueryCase):
    """An unreadable store raises StoreUnavailableError, which is exit 4."""

    def calls(self, conn: sqlite3.Connection) -> dict[str, Callable[[], Any]]:
        """Build one zero-argument call per public reader.

        Args:
            conn: The connection every reader runs on.

        Returns:
            Reader name to a call that exercises it.
        """
        return {
            "search": lambda: query.search(conn, "zebra", cwd="/repo", env={}),
            "open": lambda: query.open_event(conn, "1", roots={}),
            "sessions": lambda: query.sessions(conn, cwd="/repo"),
            "session": lambda: query.session(conn, "any"),
            "quote_check": lambda: query.quote_check(conn, "1", "zebra"),
            "caller_root": lambda: query.caller_root(
                conn, {"CODEX_THREAD_ID": "t"}
            ),
        }

    def test_reader_missing_db_exit4(self) -> None:
        missing = store.db_path(self.tmp / "nowhere")
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(missing)
        self.assertFalse(missing.parent.exists())  # a reader creates nothing
        junk = self.tmp / "junk.sqlite"
        junk.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(junk)

    def test_store_lost_mid_query_is_store_unavailable(self) -> None:
        repo = self.add_scope("/repo")
        self.add_event(self.add_source("a"), repo, "zebra")
        reader = self.ro()
        self.assertEqual(len(self.calls(reader)["search"]()["hits"]), 1)
        self.db.write_bytes(os.urandom(8192))  # the file is no database now
        for name, call in self.calls(reader).items():
            with (
                self.subTest(name),
                self.assertRaises(store.StoreUnavailableError),
            ):
                call()

    def test_locked_store_is_store_unavailable_not_a_traceback(self) -> None:
        reader = self.ro()
        reader.execute("PRAGMA busy_timeout=0")
        self.rw.execute("BEGIN EXCLUSIVE")
        try:
            for name, call in self.calls(reader).items():
                with self.subTest(name):
                    with self.assertRaises(
                        store.StoreUnavailableError
                    ) as caught:
                        call()
                    self.assertNotIn("zebra", str(caught.exception))
        finally:
            self.rw.execute("ROLLBACK")

    def test_reader_waits_through_a_writers_commit(self) -> None:
        repo = self.add_scope("/repo")
        self.add_event(self.add_source("a"), repo, "zebra")
        held, done = threading.Event(), threading.Event()

        def write() -> None:
            """Hold an exclusive transaction for a moment, then commit."""
            conn = store.connect_rw(self.db, fullfsync=False)
            try:
                conn.execute("BEGIN EXCLUSIVE")
                held.set()
                done.wait(0.6)  # the commit is a moment away
                conn.execute("COMMIT")
            finally:
                conn.close()

        writer = threading.Thread(target=write)
        writer.start()
        self.assertTrue(held.wait(5))
        start = time.monotonic()
        got = query.search(self.ro(), "zebra", cwd="/repo", env={})
        waited = time.monotonic() - start
        writer.join()
        self.assertEqual(len(got["hits"]), 1)  # answered once it committed
        self.assertGreaterEqual(waited, 0.3)

    def test_hot_journal_raises_store_unavailable_subclass(self) -> None:
        repo = self.add_scope("/repo")
        self.add_event(self.add_source("a"), repo, "zebra")
        self.rw.close()
        reader = self.ro()  # opened before the writer crashed
        self.assertEqual(len(self.calls(reader)["search"]()["hits"]), 1)
        child = Child(self, SPILLING_WRITER, self.db)
        child.wait_ready()
        child.kill()  # SIGKILL: the journal stays behind
        self.assertTrue(Path(f"{self.db}-journal").exists())
        for name, call in self.calls(reader).items():
            with (
                self.subTest(name),
                self.assertRaises(store.HotJournalError) as got,
            ):
                call()
            self.assertIsInstance(got.exception, store.StoreUnavailableError)
        with self.assertRaises(store.HotJournalError):
            store.connect_ro(self.db)  # the same class at open time
        self.assertTrue(store.heal_hot_journal(self.db, self.home))
        self.assertEqual(len(self.calls(self.ro())["search"]()["hits"]), 1)

    def test_a_bad_query_bug_is_not_disguised_as_a_store_problem(self) -> None:
        reader = self.ro()
        with self.assertRaises(sqlite3.OperationalError):  # a syntax error
            reader.execute(
                "SELECT * FROM event_fts WHERE event_fts MATCH '\"'"
            )
        with self.assertRaises(sqlite3.OperationalError):
            query.guarded(lambda: reader.execute("SELEC 1"))()


class ReadOnlyTests(OpenCase):
    """Readers leave the database file byte-identical."""

    def test_readers_never_write(self) -> None:
        _, ids, _, _ = self.write_source("thr-w", open_specs())
        self.add_event(self.add_source("k"), self.repo, "zebra text")
        self.rw.close()
        before = self.db.read_bytes()
        reader = store.connect_ro(self.db)
        self.addCleanup(reader.close)
        env = {"CODEX_THREAD_ID": "thr-w"}
        query.search(reader, "zebra text", cwd="/repo", env=env)
        query.search(reader, "zebra", cwd="/repo", env=env, all_projects=True)
        query.open_event(
            reader, cast("str", ids[3]), roots=self.roots, raw=True
        )
        query.sessions(reader, cwd="/repo")
        query.session(reader, "thr-w")
        query.quote_check(reader, cast("str", ids[0]), "first prompt")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(Path(f"{self.db}-journal").exists())
        with self.assertRaises(sqlite3.OperationalError):  # query_only
            reader.execute("DELETE FROM event")
