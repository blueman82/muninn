"""Verify rebuild recovery in fresh processes after real publication cuts."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

from muninn import cli_rebuild, erase_residue, store
from tests.cli_support import CliCase
from tests.knowledge_retirement_support import native_note
from tests.review_pause_support import initialize
from tests.test_ingest import TID

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = """
import os, sys
from muninn import cli, platform_rebuild as publication
mode = sys.argv[1]
if mode == 'fenced':
    publication.retain_store = lambda *args: os._exit(71)
elif mode == 'partial':
    def copy(source, output):
        output.write(source.read(64))
        output.flush()
        os._exit(72)
    publication.shutil.copyfileobj = copy
elif mode == 'candidate':
    publication.publish_replacement = lambda *args: os._exit(73)
elif mode == 'replaced':
    actual = publication._sync_parent
    calls = 0
    def sync(path):
        global calls
        calls += 1
        if calls == 2:
            os._exit(74)
        actual(path)
    publication._sync_parent = sync
elif mode == 'answer':
    cli._answer = lambda *args: os._exit(75)
elif mode == 'log':
    def fail(*args):
        raise OSError('synthetic log failure after publication')
    cli.obs.log_call = fail
raise SystemExit(cli.main(['rebuild']))
"""


@unittest.skipIf(sys.platform == "win32", "POSIX process publication cuts")
class RebuildCrashTests(CliCase):
    """Never infer restoration or admission authority from an absent answer."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.session(TID, "Please retain this decision.", "kept")
        self.assertEqual(self.muninn("ingest")[0], 0)
        native_note(self.home, self.roots, "durable zebra")
        self.session("erased", "Please erase erased-sentinel-long.")
        self.assertEqual(self.muninn("ingest")[0], 0)
        self.assertEqual(
            self.muninn("erase", "--match", "erased-sentinel-long", "--yes")[
                0
            ],
            0,
        )
        self.db = store.db_path(self.home)
        self.identity = self.db.stat().st_ino
        self.intent = {
            name: (self.home / name).read_bytes()
            for name in ("tombstone.key", "tombstones.jsonl")
        }

    def child(self, mode: str) -> subprocess.CompletedProcess[str]:
        """Run an isolated CLI process without inheriting authority."""
        return subprocess.run(
            [sys.executable, "-c", _SCRIPT, mode],
            env=self.env,
            cwd=_ROOT,
            close_fds=True,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def archives(self) -> dict[Path, bytes]:
        """Snapshot every enumerable retained recovery artifact."""
        return {
            Path(name): Path(name).read_bytes()
            for name in erase_residue.aside_files(self.home)
        }

    def assert_live(self, admission: int) -> None:
        """Inspect real current data and unchanged durable erasure identity."""
        for name, raw in self.intent.items():
            self.assertEqual((self.home / name).read_bytes(), raw)
        with closing(store.connect_ro(self.db)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                admission,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM knowledge_fts "
                    "WHERE knowledge_fts MATCH 'zebra'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM event "
                    "WHERE text LIKE '%erased-sentinel-long%'"
                ).fetchone()[0],
                0,
            )

    def test_prearchive_crash_leaves_admission_off_without_guessing(
        self,
    ) -> None:
        crashed = self.child("fenced")
        self.assertEqual(crashed.returncode, 71, crashed.stderr)
        self.assertEqual(crashed.stdout, "")
        self.assertEqual(self.db.stat().st_ino, self.identity)
        self.assert_live(0)
        self.assertEqual(self.archives(), {})
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())
        later = self.child("normal")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assert_live(0)
        self.assertEqual(len(self.archives()), 1)

    def test_partial_archive_crash_keeps_residue_without_restoring_it(
        self,
    ) -> None:
        crashed = self.child("partial")
        self.assertEqual(crashed.returncode, 72, crashed.stderr)
        self.assert_live(0)
        before = self.archives()
        self.assertEqual([len(raw) for raw in before.values()], [64])
        later = self.child("normal")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assert_live(0)
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_complete_orphan_is_never_promoted_by_fresh_process(self) -> None:
        crashed = self.child("candidate")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assert_live(0)
        candidate = self.home / cli_rebuild.REBUILD
        before = self.archives() | {
            self.db: self.db.read_bytes(),
            candidate: candidate.read_bytes(),
        }
        with closing(store.connect_ro(candidate)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                1,
            )
        later = self.child("normal")
        self.assertEqual(later.returncode, 2, later.stderr)
        out = json.loads(later.stdout)
        self.assertEqual(out["reason"], "rebuild_candidate_exists")
        self.assertTrue(out["publication_possible"])
        self.assertTrue(out["recovery_required"])
        self.assertNotIn("old_restored", out)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assert_live(0)

    def assert_published_crash(self, mode: str, expected: int) -> None:
        """Check new live data through a crash and fresh invocation."""
        crashed = self.child(mode)
        self.assertEqual(crashed.returncode, expected, crashed.stderr)
        self.assertEqual(crashed.stdout, "")
        self.assertNotEqual(self.db.stat().st_ino, self.identity)
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())
        self.assert_live(1)
        before = self.archives()
        self.assertEqual(len(before), 1)
        with closing(store.connect_ro(next(iter(before)))) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )
        later = self.child("normal")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assert_live(1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(len(self.archives()), 2)

    def test_replacement_before_sync_crash_keeps_new_live(self) -> None:
        self.assert_published_crash("replaced", 74)

    def test_answer_crash_does_not_imply_nonpublication(self) -> None:
        self.assert_published_crash("answer", 75)

    def test_log_failure_does_not_restore_or_reenable_old_data(self) -> None:
        self.assert_published_crash("log", 1)

    def test_absent_live_store_never_promotes_retained_archive(self) -> None:
        crashed = self.child("replaced")
        self.assertEqual(crashed.returncode, 74, crashed.stderr)
        before = self.archives()
        self.db.unlink()
        later = self.child("normal")
        self.assertNotEqual(later.returncode, 0, later.stdout)
        self.assertFalse(self.db.exists())
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        for name, raw in self.intent.items():
            self.assertEqual((self.home / name).read_bytes(), raw)
