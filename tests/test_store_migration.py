"""Exercise the ordinary upgrade route for the legacy store format."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import cutover
from install.context import StepFailedError
from install.record import Record
from muninn import store, store_fingerprint
from tests.installer_support import World
from tests.knowledge_cutover_support import legacy_schema


class StoreMigrationTests(unittest.TestCase):
    """Keep legacy rows in the new store and old ledger in its archive."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(upgrade=True)
        self.ctx.data.parent.mkdir(parents=True, exist_ok=True)
        self.ctx.data.mkdir(mode=0o700)
        self.ctx.rdir.mkdir(mode=0o700)
        self.path = store.db_path(self.ctx.data)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            legacy_schema().replace(
                "CREATE TABLE scope (", 'CREATE TABLE "scope" ('
            )
        )
        self.connection.execute(
            "INSERT INTO scope VALUES(1,'project','Project','dir')"
        )
        self.connection.execute(
            "INSERT INTO knowledge(id,scope_id,kind,text,status,actor,"
            "created_at) VALUES(1,1,'fact','old ledger','current','user',1)"
        )
        self.connection.close()
        self.path.chmod(0o600)
        self.record: Record = {"sha": self.world.sha}

    def test_upgrade_converts_versioned_store_and_retains_archive(
        self,
    ) -> None:
        with cutover.prepared_store(self.ctx, self.record) as path:
            self.assertEqual(path, self.path)
            with closing(store.connect_ro(path)) as current:
                self.assertEqual(
                    current.execute("SELECT key FROM scope").fetchone()[0],
                    "project",
                )
                self.assertEqual(
                    current.execute(
                        "SELECT count(*) FROM knowledge"
                    ).fetchone()[0],
                    0,
                )
        archive = Path(self.record["store"]["archive"])
        self.assertTrue(archive.is_file())
        with closing(
            sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True)
        ) as old:
            self.assertEqual(
                old.execute("PRAGMA user_version").fetchone()[0], 3
            )
            self.assertEqual(
                old.execute("SELECT text FROM knowledge").fetchone()[0],
                "old ledger",
            )

    def test_unknown_legacy_table_leaves_original_untouched(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE surprise(value TEXT)")
        original, identity = self.path.read_bytes(), self.path.stat().st_ino
        with self.assertRaisesRegex(
            StepFailedError, "cutover_source_mismatch"
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_ino, identity)
        self.assertFalse(self.record.get("store"))

    def test_changed_candidate_refuses_before_replacement(self) -> None:
        original, identity = self.path.read_bytes(), self.path.stat().st_ino

        capture_file = store_fingerprint.capture_file
        candidate_reads = 0

        def change_on_recheck(
            path: Path,
        ) -> store_fingerprint.FileFingerprint:
            nonlocal candidate_reads
            if "recovery-candidate-" in path.name:
                candidate_reads += 1
                if candidate_reads == 2:
                    path.write_bytes(b"changed")
            return capture_file(path)

        with (
            mock.patch.object(
                store_fingerprint,
                "capture_file",
                side_effect=change_on_recheck,
            ),
            self.assertRaisesRegex(
                StepFailedError, "cutover_candidate_changed"
            ),
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_ino, identity)
        self.assertTrue(Path(self.record["store"]["candidate_path"]).is_file())
