"""Real pinned legacy history reaches a marked store only after closure."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing
from unittest import mock

from install import installer
from install.lifecycle import linux_unit
from muninn import store
from muninn.store_copy import BASE_TABLES
from tests.knowledge_cutover_support import cursor_history, digest
from tests.legacy_cutover_support import LinuxConsumerService
from tests.legacy_upgrade_support import BOOT_A, BOOT_B, LegacyCase
from tests.store_support import insert_event, insert_source


class LegacyCutoverTests(LegacyCase):
    """Exercise the public installer across two synthetic boots."""

    def setUp(self) -> None:
        super().setUp()
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            conn.row_factory = sqlite3.Row
            source = insert_source(conn)
            insert_event(conn, source, 1, text="retained history")
            conn.commit()
        self.original = self.data_state()

    def resume(self) -> dict[str, object]:
        """Run a new installer invocation against its retained operation."""
        self.observed_boot.return_value = BOOT_B
        return installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )

    def test_later_boot_copies_history_and_retains_original_archive(
        self,
    ) -> None:
        installer.install(self.ctx, self.world.repo, self.world.sha)
        with mock.patch(
            "install.cutover.ingest_transcripts",
            side_effect=AssertionError("legacy reingest"),
        ):
            result = self.resume()
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT text FROM event").fetchall(),
                [("retained history",)],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )
        archives = list(self.ctx.data.glob("muninn.sqlite.pre-upgrade-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            archives[0].read_bytes(), self.original["muninn.sqlite"][2]
        )
        self.assertEqual(
            self.data_state()["tombstone.key"], self.original["tombstone.key"]
        )
        self.assertEqual(result["policy_status"], "activation_pending")
        self.assertTrue((self.ctx.rdir / "rollback-record.json").exists())
        self.assertFalse(self.old.exists())
        self.assertEqual(
            self.ctx.muninn.resolve(),
            self.ctx.lib / self.world.sha / "bin/muninn",
        )

    def test_pending_thread_erase_completes_in_target_without_source_write(
        self,
    ) -> None:
        journal = self.ctx.data / "tombstones.jsonl"
        journal.write_text(
            json.dumps(
                {"provider": "codex", "level": "thread", "thread_id": "t1"}
            )
            + "\n"
        )
        self.original = self.data_state()
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.resume()
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM source").fetchone()[0], 0
            )
        archive = next(self.ctx.data.glob("muninn.sqlite.pre-upgrade-*"))
        self.assertEqual(
            archive.read_bytes(), self.original["muninn.sqlite"][2]
        )
        self.assertEqual(
            journal.read_bytes(), self.original["tombstones.jsonl"][2]
        )

    def test_malformed_intent_refuses_before_archive_and_publication(
        self,
    ) -> None:
        (self.ctx.data / "tombstones.jsonl").write_bytes(
            b'{"provider":"codex"}\n'
        )
        self.original = self.data_state()
        installer.install(self.ctx, self.world.repo, self.world.sha)
        with self.assertRaisesRegex(ValueError, "erasure_intent_invalid"):
            self.resume()
        self.assert_source_retained()

    def test_linux_continuation_copies_then_starts_compatible_unit(
        self,
    ) -> None:
        self.ctx.plist.unlink()
        self.ctx = dataclasses.replace(self.ctx, platform="linux")
        self.ctx.plist.parent.mkdir(parents=True)
        self.ctx.plist.write_text(
            linux_unit(self.ctx, self.python, self.ctx.lib / "current")
        )
        self.ctx.plist.chmod(0o600)
        service = LinuxConsumerService(self)
        self.ctx.run = service
        self.observed_boot.return_value = "linux:" + BOOT_A.split(":")[1]
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.observed_boot.return_value = "linux:" + BOOT_B.split(":")[1]
        installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )
        self.assertTrue(service.active)
        self.assertEqual(self.summary()["outcome"], "ok")
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(
                conn.execute("SELECT text FROM event").fetchall()[0][0],
                "retained history",
            )

    def test_db_only_cursor_and_old_knowledge_survive_in_correct_destinations(
        self,
    ) -> None:
        with closing(sqlite3.connect(store.db_path(self.ctx.data))) as conn:
            conn.row_factory = sqlite3.Row
            cursor_history(conn)
            conn.execute(
                "INSERT INTO knowledge(id,scope_id,kind,text,status,actor,"
                "created_at) VALUES(1,1,'fact','old ledger',"
                "'current','user',1.0)"
            )
            conn.commit()
            before = digest(conn, BASE_TABLES)
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.resume()
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(digest(conn, BASE_TABLES), before)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM knowledge").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM event_fts "
                    "WHERE event_fts MATCH 'Cursor'"
                ).fetchone()[0],
                2,
            )
        with closing(
            sqlite3.connect(
                next(
                    self.ctx.data.glob("muninn.sqlite.pre-upgrade-*")
                ).as_uri()
                + "?mode=ro",
                uri=True,
            )
        ) as conn:
            self.assertEqual(
                conn.execute("SELECT text FROM knowledge").fetchone()[0],
                "old ledger",
            )
