"""The tombstone key: whole, atomic, never silently replaced.

Synthetic provider trees and a temp MUNINN_HOME only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import (
    erase,
    obs,
    platform_io,
    platform_windows,
    tombstone_key,
    tombstones,
)
from muninn.file_sync import sync_fd
from muninn.tombstone_key import KEY_FILE, TombstoneKeyError
from tests.cli_support import CliCase, fake_run
from tests.erase_support import EraseCase
from tests.store_support import assert_private
from tests.test_erase_hardening import (
    FORK,
    PARENT,
    fork_records,
    parent_records,
)
from tests.test_ingest import rollout


class KeyFileTests(unittest.TestCase):
    """Reading and creating the key file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_an_empty_or_short_key_is_refused(self) -> None:
        for size in (0, 16, 33):
            with self.subTest(size=size):
                (self.home / KEY_FILE).write_bytes(b"k" * size)
                for create in (True, False):
                    with self.assertRaises(TombstoneKeyError):
                        tombstone_key.load_key(self.home, create=create)

    def test_a_new_key_is_whole_private_and_leaves_no_temp_file(self) -> None:
        key = tombstone_key.load_key(self.home)
        path = self.home / KEY_FILE
        self.assertEqual(path.read_bytes(), key)
        self.assertEqual(len(key), 32)
        assert_private(self, path)
        self.assertEqual([p.name for p in self.home.iterdir()], [KEY_FILE])

    def test_a_missing_key_is_not_made_when_told_not_to(self) -> None:
        with self.assertRaises(TombstoneKeyError):
            tombstone_key.load_key(self.home, create=False)
        self.assertFalse((self.home / KEY_FILE).exists())

    @contextlib.contextmanager
    def winner_race(self, winner: bytes) -> Generator[None]:
        """Create a real private winner before the backend collision."""
        calls: list[str] = []
        if sys.platform == "win32":
            real_publish = platform_windows.publish

            def native(
                source: Path,
                target: Path,
                *,
                replace: bool,
                retry_move: bool = False,
                directory: bool = False,
            ) -> None:
                self.assertEqual(target, self.home / KEY_FILE)
                self.assertFalse(replace)
                fd = platform_io.open_private(
                    target, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write(winner)
                    handle.flush()
                    sync_fd(handle.fileno())
                assert_private(self, target)
                calls.append("winner")
                try:
                    real_publish(
                        source,
                        target,
                        replace=replace,
                        retry_move=retry_move,
                        directory=directory,
                    )
                except FileExistsError:
                    calls.append("collision")
                    raise
                self.fail("non-replacing publication overwrote the winner")

            with mock.patch.object(platform_windows, "publish", native):
                yield
            self.assertEqual(calls, ["winner", "collision"])
        else:
            real_link = os.link

            def raced(src: str | Path, dst: str | Path) -> None:
                Path(dst).write_bytes(winner)
                calls.append("winner")
                try:
                    real_link(src, dst)
                except FileExistsError:
                    calls.append("collision")
                    raise
                self.fail("hard link overwrote the winner")

            with mock.patch.object(os, "link", raced):
                yield
            self.assertEqual(calls, ["winner", "collision"])
        self.assertEqual((self.home / KEY_FILE).read_bytes(), winner)
        self.assertEqual([p.name for p in self.home.iterdir()], [KEY_FILE])

    def test_the_loser_of_a_creation_race_reads_the_winners_key(self) -> None:
        winner = b"w" * 32
        with self.winner_race(winner):
            got = tombstone_key.load_key(self.home)
        self.assertEqual(got, winner)

    def test_a_damaged_winner_is_not_adopted(self) -> None:
        with self.winner_race(b"short"), self.assertRaises(TombstoneKeyError):
            tombstone_key.load_key(self.home)


class LostKeyTests(EraseCase):
    """Keyed tombstones exist but their key does not."""

    def setUp(self) -> None:
        super().setUp()
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:2.1")
        self.key = self.home / KEY_FILE
        self.assertTrue(self.key.exists())

    def test_a_fork_is_not_read_with_a_new_key(self) -> None:
        self.key.unlink()
        self.write(rollout(FORK), fork_records())
        with self.assertRaises(TombstoneKeyError):
            self.run_ingest()
        self.assertFalse(self.key.exists())
        self.assertEqual(self.events(FORK), [])  # nothing half-written

    def test_a_damaged_key_stops_a_fork_too(self) -> None:
        for size in (0, 16):
            with self.subTest(size=size):
                self.key.write_bytes(b"k" * size)
                self.write(rollout(FORK), fork_records())
                with self.assertRaises(TombstoneKeyError):
                    self.run_ingest()

    def test_an_erase_that_needs_the_key_stops_before_writing(self) -> None:
        self.key.unlink()
        before = self.count("SELECT count(*) FROM tombstone")
        with self.assertRaises(TombstoneKeyError):
            self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.assertEqual(self.count("SELECT count(*) FROM tombstone"), before)
        self.assertEqual(len(self.events(PARENT)), 2)

    def test_key_problem_names_each_state(self) -> None:
        self.assertIsNone(tombstone_key.key_problem(self.home))
        self.key.write_bytes(b"k" * 16)
        self.assertEqual(tombstone_key.key_problem(self.home), "damaged")
        self.key.unlink()
        self.assertEqual(tombstone_key.key_problem(self.home), "missing")
        # The log alone is enough, as when the table was rebuilt empty.
        self.conn.execute("DELETE FROM tombstone")
        self.assertEqual(tombstone_key.key_problem(self.home), "missing")
        (self.home / tombstones.TOMBSTONE_FILE).unlink()
        self.assertIsNone(tombstone_key.key_problem(self.home))

    def test_doctor_calls_a_missing_key_an_error(self) -> None:
        self.conn.commit()
        self.key.unlink()
        with mock.patch.object(obs, "run", fake_run()):
            report = obs.doctor(self.home, self.env)
        got = {c["check"]: c for c in report["checks"]}["tombstone_key"]
        self.assertEqual((got["ok"], got["level"]), (False, "error"))
        self.assertEqual(got["detail"], "missing")
        self.assertFalse(report["ok"])

    def test_doctor_is_quiet_with_a_key_or_with_nothing_erased(self) -> None:
        with mock.patch.object(obs, "run", fake_run()):
            report = obs.doctor(self.home, self.env)
        got = {c["check"]: c for c in report["checks"]}["tombstone_key"]
        self.assertTrue(got["ok"])
        self.key.unlink()
        self.conn.execute("DELETE FROM tombstone")
        (self.home / tombstones.TOMBSTONE_FILE).unlink()
        with mock.patch.object(obs, "run", fake_run()):
            report = obs.doctor(self.home, self.env)
        got = {c["check"]: c for c in report["checks"]}["tombstone_key"]
        self.assertTrue(got["ok"])


class DryRunKeyTests(EraseCase):
    """Planning an erase must not make the key."""

    def test_a_dry_run_leaves_no_key_and_counts_like_the_real_run(
        self,
    ) -> None:
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        key = self.home / KEY_FILE
        dry = self.erase(event_ref=f"codex:{PARENT}:2.1", dry_run=True)
        self.assertFalse(key.exists())
        real = self.erase(event_ref=f"codex:{PARENT}:2.1")
        self.assertTrue(key.exists())
        self.assertEqual(dry["tombstones"], real["tombstones"])


class LogParseTests(EraseCase):
    """A damaged log row is skipped, not trusted."""

    def rows(self, *extra: dict[str, Any]) -> int:
        """Write the log with one good row plus ``extra``; reapply it."""
        good: dict[str, Any] = {
            "provider": "codex",
            "level": "line",
            "thread_id": PARENT,
            "line": 2,
            "line_sha256": "h",
            "created_at": 1.0,
        }
        text = "\n".join(json.dumps(r) for r in (good, *extra)) + "\n"
        (self.home / tombstones.TOMBSTONE_FILE).write_text(text)
        return erase.reapply_tombstones(self.conn, self.home)

    def test_bool_and_odd_fields_are_rejected(self) -> None:
        base: dict[str, Any] = {
            "provider": "codex",
            "level": "line",
            "thread_id": PARENT,
            "line_sha256": "x",
        }
        bad = [
            base | {"line": True},
            base | {"line": 3, "created_at": "yesterday"},
            base | {"line": 3, "created_at": True},
            base | {"line": 3, "level": "file"},
        ]
        self.assertEqual(self.rows(*bad), 1)  # only the good row went in

    def test_an_int_line_and_a_number_stamp_are_kept(self) -> None:
        more = {
            "provider": "claude",
            "level": "line",
            "thread_id": "t",
            "line": 7,
            "line_sha256": "y",
            "created_at": 2,
        }
        self.assertEqual(self.rows(more), 2)


class KeyCliTests(CliCase):
    """The error path: exit 4 and a content-free code."""

    def setUp(self) -> None:
        super().setUp()
        records = parent_records()
        self.write(rollout(PARENT), records)
        self.run_ingest()
        self.conn.close()
        code = self.muninn("erase", "--event", f"codex:{PARENT}:2.1", "--yes")
        self.assertEqual(code[0], 0)
        self.key = self.home / KEY_FILE
        self.key.unlink()
        self.write(rollout(FORK), fork_records())

    def test_ingest_fails_loudly(self) -> None:
        code, out, _ = self.muninn("ingest")
        self.assertEqual((code, out["error"]), (4, "tombstone_key"))
        self.assertFalse(self.key.exists())

    def test_rebuild_stops_before_replacing_anything(self) -> None:
        db = self.home / "muninn.sqlite"
        inode = db.stat().st_ino
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "tombstone_key"))
        self.assertEqual(db.stat().st_ino, inode)
        names = [p.name for p in self.home.iterdir()]
        self.assertFalse(any("rebuild" in n for n in names), names)
        self.assertFalse(self.key.exists())


if __name__ == "__main__":
    unittest.main()
