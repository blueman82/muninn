"""Store contract: paths, readers, hot journals and the writer lock.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from pathlib import Path
from typing import NoReturn
from unittest import mock

from muninn import store
from tests.store_support import (
    HOLD_LOCK,
    SPILLING_WRITER,
    Child,
    StoreCase,
    insert_scope,
    mode,
)

# Re-exported because other test modules still import these helpers from
# here rather than from tests.store_support.
__all__ = ["SPILLING_WRITER", "Child"]


class PathTests(StoreCase):
    """Data dir resolution and atomic private file writes."""

    def test_data_home_env_default_and_expansion(self) -> None:
        default = Path.home() / ".local" / "share" / "muninn"
        self.assertEqual(
            store.data_home({"MUNINN_HOME": "/x/y"}), Path("/x/y")
        )
        self.assertEqual(store.data_home({}), default)
        self.assertEqual(store.data_home({"MUNINN_HOME": ""}), default)
        self.assertEqual(
            store.data_home({"MUNINN_HOME": "~/pc"}), Path.home() / "pc"
        )

    def test_db_path(self) -> None:
        self.assertEqual(store.db_path(Path("/h")), Path("/h/muninn.sqlite"))

    def test_ensure_private_dir_creates_and_tightens(self) -> None:
        target = self.tmp / "a" / "b"
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)
        target.chmod(0o755)
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)

    def test_write_json_atomic_is_private_and_replaces(self) -> None:
        target = self.tmp / "status.json"
        target.write_text("{}")
        target.chmod(0o644)
        store.write_json_atomic(target, {"b": 2, "a": [1]})
        self.assertEqual(target.read_text(), '{"a": [1], "b": 2}')  # sorted
        self.assertEqual(mode(target), 0o600)

    def test_write_json_atomic_failure_keeps_old_and_leaves_no_temp(
        self,
    ) -> None:
        target = self.tmp / "status.json"
        store.write_json_atomic(target, {"ok": True})
        with self.assertRaises(TypeError):  # unserialisable: no temp file
            store.write_json_atomic(target, {"x": object()})
        blocker = self.tmp / "blocked"
        blocker.mkdir()  # os.replace onto a directory fails after writing
        with self.assertRaises(OSError):
            store.write_json_atomic(blocker, {"ok": True})
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()),
            ["blocked", "status.json"],
        )

    def test_an_interrupt_before_the_rename_leaves_no_temp_file(self) -> None:
        target = self.tmp / "status.json"
        store.write_json_atomic(target, {"old": True})

        def interrupt(src: Path, dst: Path) -> NoReturn:
            raise KeyboardInterrupt

        with (
            mock.patch.object(Path, "replace", interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            store.write_json_atomic(target, {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"old": True})
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), ["status.json"]
        )

    def test_an_interrupt_after_the_rename_is_not_masked(self) -> None:
        target = self.tmp / "status.json"
        real_replace = Path.replace

        def replace_then_interrupt(src: Path, dst: Path) -> NoReturn:
            real_replace(src, dst)
            raise KeyboardInterrupt  # a signal handler firing just after

        with (
            mock.patch.object(Path, "replace", replace_then_interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            store.write_json_atomic(target, {"ok": True})
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), ["status.json"]
        )


class ReaderTests(StoreCase):
    """Read-only connections and their failure modes."""

    def test_connect_ro_missing_raises_store_unavailable(self) -> None:
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(self.db)
        self.assertFalse(self.db.exists())

    def assert_plain_unavailable(self) -> None:
        """Assert that opening the reader fails without a hot journal.

        Raises:
            AssertionError: If the failure is a hot-journal error.
        """
        with self.assertRaises(store.StoreUnavailableError) as caught:
            store.connect_ro(self.db)
        self.assertNotIsInstance(caught.exception, store.HotJournalError)

    def test_connect_ro_rejects_uninitialised_and_foreign_schema(self) -> None:
        store.ensure_private_dir(self.home)
        self.db.touch()  # zero bytes: no writer has created the schema yet
        self.assert_plain_unavailable()
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version=3")  # written by a newer muninn
        raw.close()
        self.assert_plain_unavailable()

    def test_connect_ro_is_query_only(self) -> None:
        self.rw().close()
        conn = self.ro()
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000
        )
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO scope(key, label, kind) VALUES ('a','a','dir')"
            )

    def test_readers_and_writers_return_named_rows(self) -> None:
        writer = self.rw()
        insert_scope(writer, "/named")
        row = self.ro().execute("SELECT key, kind FROM scope").fetchone()
        self.assertEqual((row["key"], row["kind"]), ("/named", "git"))


class HotJournalTests(StoreCase):
    """Detection and healing of a journal left by a killed writer."""

    def leave_hot_journal(self) -> None:
        """Kill a spilled writer mid-transaction so a journal remains."""
        committed = self.rw()
        insert_scope(committed, "/committed")
        committed.close()
        child = Child(self, SPILLING_WRITER, self.db)
        child.wait_ready()
        child.kill()  # SIGKILL: the journal stays behind
        self.assertTrue(Path(f"{self.db}-journal").exists())

    def test_connect_ro_hot_journal_raises_hotjournal(self) -> None:
        self.leave_hot_journal()
        with self.assertRaises(store.HotJournalError) as caught:
            store.connect_ro(self.db)
        self.assertIsInstance(caught.exception, store.StoreUnavailableError)

    def test_heal_hot_journal_rolls_back_uncommitted_writes(self) -> None:
        self.leave_hot_journal()
        self.assertTrue(store.heal_hot_journal(self.db, self.home))
        self.assertFalse(Path(f"{self.db}-journal").exists())
        keys = [r["key"] for r in self.ro().execute("SELECT key FROM scope")]
        self.assertEqual(keys, ["/committed"])
        quick = self.ro().execute("PRAGMA quick_check").fetchone()[0]
        self.assertEqual(quick, "ok")

    def test_heal_hot_journal_false_while_writer_lock_is_held(self) -> None:
        self.leave_hot_journal()
        with store.writer_lock(self.home):
            self.assertFalse(store.heal_hot_journal(self.db, self.home))
        self.assertTrue(Path(f"{self.db}-journal").exists())
        with self.assertRaises(store.HotJournalError):
            store.connect_ro(self.db)
        self.assertTrue(store.heal_hot_journal(self.db, self.home))

    def test_heal_hot_journal_missing_db_returns_false(self) -> None:
        store.ensure_private_dir(self.home)
        self.assertFalse(store.heal_hot_journal(self.db, self.home))
        self.assertFalse(self.db.exists())


class WriterLockTests(StoreCase):
    """Cross-process writer lock behaviour."""

    def test_writer_lock_busy(self) -> None:
        holder = Child(self, HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        with (
            self.assertRaises(store.BusyError),
            store.writer_lock(self.home, wait_s=0),
        ):
            self.fail("lock was granted while another process held it")
        holder.kill()  # SIGKILL releases the flock: no stale lock
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_writer_lock_times_out_after_wait_s(self) -> None:
        holder = Child(self, HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        start = time.monotonic()
        with (
            self.assertRaises(store.BusyError),
            store.writer_lock(self.home, wait_s=0.3),
        ):
            pass
        elapsed = time.monotonic() - start
        self.assertTrue(0.25 <= elapsed < 5, elapsed)

    def test_writer_lock_waits_for_release_then_acquires(self) -> None:
        holder = Child(self, HOLD_LOCK, self.home, 0.6)
        holder.wait_ready()
        start = time.monotonic()
        with store.writer_lock(self.home, wait_s=15):
            waited = time.monotonic() - start
        self.assertGreater(waited, 0.2)
        self.assertEqual(holder.proc.wait(timeout=10), 0)

    def test_writer_lock_is_not_reentrant_in_process(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaises(store.BusyError),
            store.writer_lock(self.home, wait_s=0),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
