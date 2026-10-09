"""Store contract: paths, readers, hot journals and the writer lock.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from typing import NoReturn
from unittest import mock

from muninn import platform_io, platform_windows, store
from tests import store_support
from tests.store_support import (
    HOLD_LOCK,
    SPILLING_WRITER,
    Child,
    StoreCase,
    assert_private,
    insert_scope,
    public_read,
)

# Re-exported because other test modules still import these helpers from
# here rather than from tests.store_support.
__all__ = ["SPILLING_WRITER", "Child"]


class PathTests(StoreCase):
    """Data dir resolution and atomic private file writes."""

    def test_data_home_env_default_and_expansion(self) -> None:
        default = (
            Path.home() / "AppData" / "Local" / "Muninn" / "data"
            if sys.platform == "win32"
            else Path.home() / ".local" / "share" / "muninn"
        )
        self.assertEqual(
            store.data_home({"MUNINN_HOME": "/x/y"}), Path("/x/y")
        )
        self.assertEqual(store.data_home({}), default)
        self.assertEqual(store.data_home({"MUNINN_HOME": ""}), default)
        self.assertEqual(
            store.data_home({"MUNINN_HOME": "~/pc"}), Path.home() / "pc"
        )

    def test_shared_privacy_control_rejects_public_file_and_directory(
        self,
    ) -> None:
        target = self.tmp / "private"
        store.ensure_private_dir(target)
        path = target / "status.json"
        store.write_json_atomic(path, {"unchanged": True})
        original = path.read_bytes()
        for value, directory in ((path, False), (target, True)):
            with self.subTest(directory=directory):
                store_support.assert_private(self, value, directory=directory)
                with store_support.public_read(
                    self, value, directory=directory
                ):
                    self.assertFalse(
                        platform_io.is_private(value, directory=directory)
                    )
                    with self.assertRaises(AssertionError):
                        store_support.assert_private(
                            self, value, directory=directory
                        )
                    self.assertEqual(path.read_bytes(), original)
                store_support.assert_private(self, value, directory=directory)
        self.assertEqual(path.read_bytes(), original)

    def test_db_path(self) -> None:
        self.assertEqual(store.db_path(Path("/h")), Path("/h/muninn.sqlite"))

    def test_ensure_private_dir_creates_and_handles_lax_existing_state(
        self,
    ) -> None:
        target = self.tmp / "a" / "b"
        store.ensure_private_dir(target)
        assert_private(self, target, directory=True)
        if sys.platform == "win32":
            marker = target / "unchanged.txt"
            marker.write_bytes(b"unchanged")
            with public_read(self, target, directory=True):
                with self.assertRaises(PermissionError):
                    store.ensure_private_dir(target)
                self.assertEqual(marker.read_bytes(), b"unchanged")
        else:
            target.chmod(0o755)
            store.ensure_private_dir(target)
        assert_private(self, target, directory=True)

    def test_write_json_atomic_is_private_and_replaces(self) -> None:
        target = self.tmp / "status.json"
        if sys.platform == "win32":
            store.write_json_atomic(target, {})
            with public_read(self, target):
                original = target.read_bytes()
                with self.assertRaises(PermissionError):
                    store.write_json_atomic(target, {"unsafe": True})
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(
                    sorted(p.name for p in self.tmp.iterdir()), ["status.json"]
                )
        else:
            target.write_text("{}")
            target.chmod(0o644)
        store.write_json_atomic(target, {"b": 2, "a": [1]})
        self.assertEqual(target.read_text(), '{"a": [1], "b": 2}')  # sorted
        assert_private(self, target)

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

    def test_an_interrupt_before_publication_leaves_no_temp_file(self) -> None:
        target = self.tmp / "status.json"
        store.write_json_atomic(target, {"old": True})
        calls: list[Path] = []

        def interrupt(src: Path, dst: Path) -> NoReturn:
            calls.append(dst)
            raise KeyboardInterrupt

        if sys.platform == "win32":

            def native(
                src: Path,
                dst: Path,
                *,
                replace: bool,
                retry_move: bool = False,
                directory: bool = False,
            ) -> NoReturn:
                self.assertTrue(replace)
                self.assertTrue(retry_move)
                self.assertFalse(directory)
                assert_private(self, src)
                interrupt(src, dst)

            patch = mock.patch.object(platform_windows, "publish", native)
        else:
            patch = mock.patch.object(Path, "replace", interrupt)
        with patch, self.assertRaises(KeyboardInterrupt):
            store.write_json_atomic(target, {"new": True})
        self.assertEqual(calls, [target])
        self.assertEqual(json.loads(target.read_text()), {"old": True})
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), ["status.json"]
        )
        assert_private(self, target)

    def test_an_interrupt_after_publication_is_not_masked(self) -> None:
        target = self.tmp / "status.json"
        calls: list[Path] = []
        real_replace = Path.replace

        def replace_then_interrupt(src: Path, dst: Path) -> NoReturn:
            real_replace(src, dst)
            calls.append(dst)
            raise KeyboardInterrupt

        if sys.platform == "win32":
            real_publish = platform_windows.publish

            def native(
                src: Path,
                dst: Path,
                *,
                replace: bool,
                retry_move: bool = False,
                directory: bool = False,
            ) -> NoReturn:
                self.assertTrue(replace)
                self.assertTrue(retry_move)
                self.assertFalse(directory)
                real_publish(
                    src,
                    dst,
                    replace=replace,
                    retry_move=retry_move,
                    directory=directory,
                )
                calls.append(dst)
                raise KeyboardInterrupt

            patch = mock.patch.object(platform_windows, "publish", native)
        else:
            patch = mock.patch.object(Path, "replace", replace_then_interrupt)
        with patch, self.assertRaises(KeyboardInterrupt):
            store.write_json_atomic(target, {"ok": True})
        self.assertEqual(calls, [target])
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), ["status.json"]
        )
        assert_private(self, target)


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
        raw.execute("PRAGMA user_version=4")  # written by a newer muninn
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
