"""Exercise the ordinary upgrade route for the legacy store format."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import cutover, learning_scope, recovery_guidance
from install.context import StepFailedError
from install.record import Record
from muninn import store, store_copy, store_fingerprint
from tests.installer_support import World
from tests.knowledge_cutover_support import legacy_schema
from tests.policy_preview_support import states


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

    def test_installer_pause_check_keeps_historical_migration_route(
        self,
    ) -> None:
        """Let v3 stores reach their dedicated migration before recovery."""
        original = states(self.ctx.data)
        with mock.patch.object(
            recovery_guidance.cli_recovery,
            "recover",
            side_effect=AssertionError("current-format recovery called"),
        ):
            recovery_guidance.recover_pause(self.ctx, self.record)
        self.assertEqual(states(self.ctx.data), original)
        with cutover.prepared_store(self.ctx, self.record) as path:
            self.assertEqual(path, self.path)

    def test_legacy_scope_choice_needs_no_current_writer_lock(self) -> None:
        original = states(self.ctx.data)
        learning_scope.choose(self.ctx, "now", lambda question: "yes")
        self.assertEqual(self.ctx.learning_mode, "now")
        self.assertEqual(states(self.ctx.data), original)

    def test_early_legacy_route_refuses_unknown_or_marked_tables(self) -> None:
        for table in ("surprise", "store_meta"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.path)) as conn:
                    conn.execute(f"CREATE TABLE {table}(value TEXT)")
                original = states(self.ctx.data)
                with self.assertRaisesRegex(
                    StepFailedError, "cutover_source_mismatch"
                ):
                    recovery_guidance.recover_pause(self.ctx, self.record)
                self.assertEqual(states(self.ctx.data), original)
                self.assertFalse(self.record.get("store"))
                with closing(sqlite3.connect(self.path)) as conn:
                    conn.execute(f"DROP TABLE {table}")

    def test_marked_one_without_lock_still_refuses_early_reads(self) -> None:
        self.path.unlink()
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "installer"
            / "marked-v1-base.sql"
        )
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(fixture.read_text())
        self.path.chmod(0o600)
        original = states(self.ctx.data)
        for action in (
            lambda: recovery_guidance.recover_pause(self.ctx, self.record),
            lambda: learning_scope.choose(
                self.ctx, "now", lambda question: "yes"
            ),
        ):
            with self.assertRaisesRegex(
                StepFailedError,
                "review_policy_preview_unavailable: state_missing",
            ):
                action()
            self.assertEqual(states(self.ctx.data), original)
            self.assertFalse(self.record.get("store"))

    def test_changed_marked_header_cannot_use_the_legacy_route(self) -> None:
        self.path.unlink()
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("PRAGMA user_version=3")
        original = states(self.ctx.data)
        for action in (
            lambda: recovery_guidance.recover_pause(self.ctx, self.record),
            lambda: learning_scope.choose(
                self.ctx, "now", lambda question: "yes"
            ),
        ):
            with self.assertRaisesRegex(
                StepFailedError, "cutover_source_mismatch"
            ):
                action()
            self.assertEqual(states(self.ctx.data), original)
            self.assertFalse(self.record.get("store"))

    def test_legacy_sidecars_never_gain_a_lockless_bypass(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            for linked in (False, True):
                with self.subTest(suffix=suffix, linked=linked):
                    sidecar = Path(f"{self.path}{suffix}")
                    if linked:
                        sidecar.symlink_to(self.ctx.data / "absent")
                    else:
                        sidecar.write_bytes(b"pending private state")
                        sidecar.chmod(0o600)
                    original = states(self.ctx.data)
                    for action in (
                        lambda: recovery_guidance.recover_pause(
                            self.ctx, self.record
                        ),
                        lambda: learning_scope.choose(
                            self.ctx, "now", lambda question: "yes"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            StepFailedError, "cutover_source_mismatch"
                        ):
                            action()
                        self.assertEqual(states(self.ctx.data), original)
                        self.assertFalse(self.record.get("store"))
                    sidecar.unlink()

    def test_legacy_uncheckpointed_wal_is_not_a_settled_source(self) -> None:
        with closing(sqlite3.connect(self.path)) as writer:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            writer.execute("UPDATE knowledge SET text='pending old claim'")
            writer.commit()
            self.assertGreater(Path(f"{self.path}-wal").stat().st_size, 0)
            original = states(self.ctx.data)
            with self.assertRaisesRegex(
                StepFailedError, "cutover_source_mismatch"
            ):
                recovery_guidance.recover_pause(self.ctx, self.record)
            self.assertEqual(states(self.ctx.data), original)
            self.assertFalse(self.record.get("store"))

    def test_changed_legacy_file_is_refused_after_real_validation(
        self,
    ) -> None:
        validate = store_copy.validate_source
        observed: list[bytes] = []

        def change_after_validation(conn: sqlite3.Connection) -> int:
            result = validate(conn)
            with self.path.open("ab") as handle:
                handle.write(b"changed after real inspection")
            observed.append(self.path.read_bytes())
            return result

        with (
            mock.patch.object(
                store_copy, "validate_source", change_after_validation
            ),
            self.assertRaisesRegex(StepFailedError, "cutover_source_mismatch"),
        ):
            recovery_guidance.recover_pause(self.ctx, self.record)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.path.read_bytes(), observed[0])
        self.assertFalse((self.ctx.data / "writer.lock").exists())
        self.assertFalse(self.record.get("store"))

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
