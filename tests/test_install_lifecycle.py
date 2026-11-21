"""Service definitions preserve transaction-safe stop and user ownership."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

from install import installer
from muninn import store
from tests import test_install_cursor_ingest


class LifecycleDefinitionTests(unittest.TestCase):
    """Check the effective settings rather than a backend class hierarchy."""

    def test_cleanup_failure_preserves_scrub_in_failed_install(self) -> None:
        fixture = test_install_cursor_ingest.FreshCursorReplayTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        world = fixture.fixture.world
        fixture.ctx.rdir.rmdir()
        with (
            fixture.operation("query"),
            self.assertRaises(sqlite3.DatabaseError),
        ):
            installer.install(fixture.ctx, world.repo, world.sha)
        self.assertIsNone(world.fake.loaded)
        self.assertFalse((fixture.ctx.lib / "current").exists())
        retained = fixture.ctx.failed / "data"
        with closing(store.connect_ro(store.db_path(retained))) as conn:
            fixture.assert_scrubbed(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM source WHERE provider='cursor'"
                ).fetchone()[0],
                0,
            )
        self.assertTrue(
            (retained / "review-work" / fixture.work.name).is_dir()
        )
        self.assertFalse((retained / "status.json").exists())

    def test_successful_install_finishes_with_service_running(self) -> None:
        fresh = [step.__name__ for step in installer.FRESH_STEPS]
        upgrade = [step.__name__ for step in installer.UPGRADE_STEPS]
        self.assertNotIn("pause", fresh)
        self.assertEqual(upgrade.count("pause"), 1)
        self.assertLess(upgrade.index("pause"), upgrade.index("prepare_store"))
        self.assertEqual(fresh[-1], "prune")
        self.assertEqual(upgrade[-1], "prune")
