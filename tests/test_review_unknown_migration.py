"""Archive an old marked store before its additive metadata migration."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import current_store
from install.record import Record
from muninn import review_pause as pause
from muninn import review_state, review_unknown_state, store, store_copy
from muninn.store_schema import SCHEMA_SQL
from tests.installer_support import World
from tests.review_pause_support import initialize


class UnknownMigrationTests(unittest.TestCase):
    """Exercise real old DDL, archive bytes and new-table publication."""

    def test_upgrade_archives_old_shape_before_creating_auxiliary_table(
        self,
    ) -> None:
        world = World(self)
        ctx = world.ctx(upgrade=True)
        ctx.data.mkdir(parents=True, mode=0o700)
        ctx.rdir.mkdir(mode=0o700)
        path = store.db_path(ctx.data)
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            conn.executescript(
                SCHEMA_SQL.replace(review_unknown_state.ACK_SQL, "", 1)
            )
            conn.execute("PRAGMA user_version=2")
            conn.execute(
                "INSERT INTO scope VALUES(1,'legacy-marker','retained','dir')"
            )
            self.assertEqual(store_copy.validate_source(conn), 2)
        path.chmod(0o600)
        initialize(ctx.data)
        record: Record = {}
        opened: list[sqlite3.Connection] = []
        connect = store.connect_rw
        retain = current_store.platform_rebuild.retain_upgrade_store

        def track(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            opened.append(conn)
            return conn

        def closed_archive(home: Path, source: Path) -> Path:
            self.assertTrue(opened)
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            return retain(home, source)

        with (
            pause.maintenance_owner(
                ctx.data, wait_s=0, status=pause.EraseStatus()
            ) as owner,
            mock.patch.object(store, "connect_rw", side_effect=track),
            mock.patch.object(
                current_store.platform_rebuild,
                "retain_upgrade_store",
                side_effect=closed_archive,
            ),
        ):
            self.assertEqual(
                current_store.prepare_current(ctx, record, owner), path
            )
        with closing(store.connect_ro(path)) as conn:
            self.assertTrue(review_state.available(conn, "review_unknown_ack"))
            self.assertEqual(store_copy.validate_source(conn), 2)
            self.assertEqual(
                conn.execute("SELECT key FROM scope").fetchone()[0],
                "legacy-marker",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )
        archive = Path(record["store"]["archive"])
        with closing(store.connect_ro(archive)) as conn:
            self.assertFalse(
                review_state.available(conn, "review_unknown_ack")
            )
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT key FROM scope").fetchone()[0],
                "legacy-marker",
            )

    def test_copy_accepts_an_old_empty_shape_and_keeps_new_metadata_empty(
        self,
    ) -> None:
        with (
            closing(sqlite3.connect(":memory:", isolation_level=None)) as old,
            closing(sqlite3.connect(":memory:", isolation_level=None)) as new,
        ):
            old.row_factory = new.row_factory = sqlite3.Row
            old.executescript(
                SCHEMA_SQL.replace(review_unknown_state.ACK_SQL, "", 1)
            )
            old.execute("PRAGMA user_version=2")
            store.initialize_store(new)
            store_copy.copy_governed_state(old, new)
            self.assertEqual(
                new.execute(
                    "SELECT count(*) FROM review_unknown_ack"
                ).fetchone()[0],
                0,
            )
