"""Preserve marked stores and archives across isolated installer failures."""

from __future__ import annotations

import dataclasses
import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import cutover
from install import installer as co
from install import snapshot as sn
from install.constants import PRE_UPGRADE_PREFIX
from muninn import erase_residue, obs, platform_io, platform_rebuild, store
from muninn import review_pause as q
from muninn import review_pause_state as state
from tests.cli_support import fake_run
from tests.installer_support import ROOT, World, git, make_store
from tests.store_support import assert_private

TS = "20261002T000000Z"


def marked_store(data: Path, version: int, marker: str = "row") -> Path:
    """Model ingest with a real marked store and a durable scope row."""
    assert version == store.SCHEMA_VERSION
    path = store.db_path(data)
    with closing(store.connect_rw(path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scope(key,label,kind) VALUES(?,?,'dir')",
            (marker, marker),
        )
    return path


def rows(path: Path) -> tuple[tuple[object, ...], ...]:
    """Read every durable scope marker independently of installer records."""
    with closing(store.connect_ro(path)) as conn:
        return tuple(tuple(row) for row in conn.execute("SELECT * FROM scope"))


class SnapshotCase(unittest.TestCase):
    """Own a never-exposed temp home with only fake service consumers."""

    def setUp(self) -> None:
        self.w = w = World(self)
        patch = mock.patch("tests.installer_support.make_store", marked_store)
        patch.start()
        self.addCleanup(patch.stop)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("next\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "next")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.ctx = dataclasses.replace(w.ctx(upgrade=True), ts=TS)
        self.lib, self.data = self.ctx.lib, self.ctx.data
        self.store = store.db_path(self.data)
        self.snap = self.data / f"{PRE_UPGRADE_PREFIX}{self.sha2[:12]}"

    def retained_copy(self) -> Path:
        """Create actual private recovery evidence beneath fixture barriers."""
        with (
            q.maintenance_owner(
                self.data, wait_s=0, status=q.EraseStatus()
            ) as owner,
            q.maintenance_writer(owner),
        ):
            with closing(store.connect_rw(self.store)) as conn:
                q.verify_current(owner, conn)
            return platform_rebuild.retain_store(self.data, self.store)

    def record(self) -> co.Record:
        """Read the durable public installer summary."""
        return json.loads((self.lib / co.INSTALL_RECORD).read_text())

    def selection(self) -> Path:
        """Read the release actually selected by the fixture launcher."""
        return (self.lib / "current").readlink()


class UpgradeFlowTests(SnapshotCase):
    """Current maintenance has no format-copy or archive-deletion step."""

    def test_fresh_initializes_no_policy_before_consumers(self) -> None:
        with closing(store.connect_ro(self.store)) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM review_control").fetchone()),
                (1, 0, 0, None, None, None),
            )
        with (
            q.maintenance_owner(
                self.data, wait_s=0, status=q.EraseStatus()
            ) as owner,
            q.maintenance_writer(owner),
            closing(store.connect_rw(self.store)) as conn,
        ):
            q.verify_current(owner, conn)
        self.assertTrue((self.data / state.WITNESS).is_file())
        self.assertLess(
            co.FRESH_STEPS.index(co.prepare_store),
            co.FRESH_STEPS.index(co.pin),
        )

    def test_current_upgrade_keeps_file_key_and_existing_archive(self) -> None:
        self.snap.write_bytes(b"existing recoverable archive")
        self.snap.chmod(0o600)
        key = self.data / "tombstone.key"
        key.write_bytes(b"unchanged synthetic durable key")
        key.chmod(0o600)
        before = self.store.stat().st_ino, rows(self.store)
        with mock.patch.object(platform_rebuild, "retain_store") as copy:
            co.install(self.ctx, self.w.repo, self.sha2)
        copy.assert_not_called()
        self.assertEqual((self.store.stat().st_ino, rows(self.store)), before)
        self.assertEqual(key.read_bytes(), b"unchanged synthetic durable key")
        self.assertEqual(
            self.snap.read_bytes(), b"existing recoverable archive"
        )
        self.assertEqual(self.selection(), Path(self.sha2))
        self.assertEqual(self.record()["store"]["phase"], "current_prepared")
        self.assertEqual(self.record()["outcome"], "ok")

    def test_native_stop_precedes_owner_and_restart_follows_release(
        self,
    ) -> None:
        acquire = q.maintenance_owner

        def stopped(home: Path, **kwargs: object) -> object:
            self.assertIsNone(self.w.fake.loaded)
            return acquire(home, wait_s=0, status=q.EraseStatus())

        with mock.patch.object(q, "maintenance_owner", side_effect=stopped):
            co.install(self.ctx, self.w.repo, self.sha2)
        os.close(state.acquire(self.data))
        with store.writer_lock(self.data, wait_s=0):
            pass
        self.assertEqual(self.w.fake.loaded, "new")
        calls = self.w.fake.calls
        stop = ["launchctl", "bootout", self.ctx.target]
        start = [
            "launchctl",
            "bootstrap",
            f"gui/{self.ctx.uid}",
            str(self.ctx.plist),
        ]
        self.assertLess(
            calls.index(stop), len(calls) - 1 - calls[::-1].index(start)
        )

    def test_owner_spans_pin_and_config_without_holding_writer(self) -> None:
        observed: list[str] = []

        def checked(ctx: co.Ctx, rec: co.Record) -> None:
            try:
                fd = state.acquire(ctx.data)
            except BlockingIOError:
                pass
            else:
                os.close(fd)
                self.fail("maintenance owner released before pin/config")
            with store.writer_lock(ctx.data, wait_s=0):
                observed.append("pin/config")

        steps = list(co.UPGRADE_STEPS)
        steps.insert(steps.index(co.pin), checked)
        steps.insert(steps.index(co.restart), checked)
        with mock.patch.object(co, "UPGRADE_STEPS", tuple(steps)):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(observed, ["pin/config", "pin/config"])
        os.close(state.acquire(self.data))

    def test_copy_is_private_consistent_and_does_not_consume_source(
        self,
    ) -> None:
        before = self.store.read_bytes()
        self.snap = self.retained_copy()
        assert_private(self, self.snap)
        self.assertEqual(rows(self.snap), rows(self.store))
        self.assertEqual(self.store.read_bytes(), before)

    def test_existing_private_copy_is_never_overwritten(self) -> None:
        self.snap = self.data / (store.RECOVERY_PREFIX + "collision")
        self.snap.write_bytes(b"unknown prior snapshot")
        before = self.store.read_bytes()
        with mock.patch.object(platform_rebuild.uuid, "uuid4") as ident:
            ident.return_value.hex = "collision"
            with self.assertRaises(FileExistsError):
                self.retained_copy()
        self.assertEqual(self.snap.read_bytes(), b"unknown prior snapshot")
        self.assertEqual(self.store.read_bytes(), before)

    def test_record_failure_aborts_before_material_mutation(self) -> None:
        before = self.store.read_bytes(), self.ctx.config.read_bytes()
        failure = OSError("record is not durable")
        with (
            mock.patch.object(cutover.record, "save", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertIs(caught.exception, failure)
        self.assertEqual(
            (self.store.read_bytes(), self.ctx.config.read_bytes()), before
        )
        self.assertEqual(self.selection(), Path(self.first))
        self.assertIsNone(self.w.fake.loaded)
        self.assertTrue((self.lib / self.sha2 / ".venv").is_dir())

    def test_unreadable_store_refuses_without_changing_content_or_selection(
        self,
    ) -> None:
        self.store.write_bytes(b"not a database" * 100)
        before = self.store.read_bytes(), self.ctx.config.read_bytes()
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(
            (self.store.read_bytes(), self.ctx.config.read_bytes()), before
        )
        self.assertEqual(self.selection(), Path(self.first))
        self.assertIsNone(self.w.fake.loaded)

    def test_historical_numeric_one_is_refused_without_migration(self) -> None:
        self.store.unlink()
        make_store(self.data, 1)
        before = self.store.read_bytes()
        with self.assertRaisesRegex(
            co.StepFailedError, "cutover_source_mismatch"
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.selection(), Path(self.first))
        self.assertIsNone(self.w.fake.loaded)

    def test_retained_archive_is_reported_and_never_discarded(self) -> None:
        self.snap.write_bytes(b"recoverable private data")
        rec: co.Record = {"snapshot": {"name": self.snap.name}}
        self.assertTrue(sn.discard(self.ctx, rec))
        self.assertEqual(self.snap.read_bytes(), b"recoverable private data")
        self.assertEqual(rec["snapshot"]["state"], "retained")
        self.assertEqual(
            erase_residue.aside_files(self.data), [str(self.snap)]
        )
        with mock.patch.object(obs, "run", fake_run()):
            checks = {
                c["check"]: c
                for c in obs.doctor(self.data, {"HOME": "/x"})["checks"]
            }
        self.assertIs(checks["upgrade_snapshot"]["ok"], False)
        self.assertEqual(checks["upgrade_snapshot"]["level"], "warn")
        self.assertIs(checks["unexpected_files"]["ok"], True)


class RollbackTests(SnapshotCase):
    """Once current data may change, retain it and keep the service stopped."""

    def assert_retained(self) -> None:
        """Check recovery never points an old runtime at current state."""
        self.assertEqual(self.selection(), Path(self.sha2))
        self.assertTrue((self.lib / self.first).is_dir())
        self.assertTrue((self.lib / self.sha2).is_dir())
        self.assertIsNone(self.w.fake.loaded)
        self.assertEqual(self.record()["outcome"], "recovery_required")
        assert_private(self, self.store)

    def test_failed_verify_retains_current_store_and_runtime_selection(
        self,
    ) -> None:
        self.w.fake.doctor = 1
        before = rows(self.store)
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(rows(self.store), before)
        self.assert_retained()

    def test_failed_restart_retains_current_store_and_runtime_selection(
        self,
    ) -> None:
        self.w.fake.heartbeat = False
        self.w.fake.clock[0] += 1000
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assert_retained()

    def test_after_new_writes_blind_restore_refused(self) -> None:
        self.snap = self.retained_copy()
        before = self.snap.read_bytes()
        configs: list[bytes] = []

        def new_write(ctx: co.Ctx, rec: co.Record) -> None:
            marked_store(ctx.data, 2, "newer")
            configs.append(ctx.config.read_bytes())
            raise co.StepFailedError("verification failed after exposure")

        steps = tuple(
            new_write if step is co.verify else step
            for step in co.UPGRADE_STEPS
        )
        with (
            mock.patch.object(co, "UPGRADE_STEPS", steps),
            self.assertRaises(co.StepFailedError),
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertIn("newer", [row[1] for row in rows(self.store)])
        self.assertEqual(self.snap.read_bytes(), before)
        self.assertEqual(self.ctx.config.read_bytes(), configs[0])
        self.assert_retained()

    def test_residual_journal_retains_both_stores_and_selection(self) -> None:
        self.snap = self.retained_copy()
        saved = self.snap.read_bytes()
        written: list[bytes] = []
        journal = self.data / "muninn.sqlite-journal"

        def interrupted(ctx: co.Ctx, rec: co.Record) -> None:
            marked_store(ctx.data, 2, "newer")
            written.append(self.store.read_bytes())
            fd = platform_io.open_private(
                journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"stale")
            raise co.StepFailedError("verification interrupted")

        steps = tuple(
            interrupted if step is co.verify else step
            for step in co.UPGRADE_STEPS
        )
        with (
            mock.patch.object(co, "UPGRADE_STEPS", steps),
            self.assertRaises(co.StepFailedError),
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(self.store.read_bytes(), written[0])
        self.assertEqual(self.snap.read_bytes(), saved)
        self.assertEqual(journal.read_bytes(), b"stale")
        self.assert_retained()


class PlanAndNamesTests(SnapshotCase):
    """Dry run and archive naming stay consistent with runtime diagnostics."""

    def test_dry_run_says_so_and_writes_nothing(self) -> None:
        before = {
            p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()
        }
        ctx = dataclasses.replace(self.ctx, dry_run=True)
        self.w.out.clear()
        co.install(ctx, self.w.repo, self.sha2)
        self.assertIn(
            "would prepare the governed store", "\n".join(self.w.out)
        )
        self.assertEqual(
            {
                p.name: p.read_bytes()
                for p in self.data.iterdir()
                if p.is_file()
            },
            before,
        )

    def test_release_schema_matches_the_runtime_constant(self) -> None:
        self.assertEqual(
            sn.release_schema(self.ctx, self.w.repo, self.sha2),
            store.SCHEMA_VERSION,
        )
        found = sn._VERSION.findall((ROOT / sn.SCHEMA_FILE).read_bytes())
        self.assertEqual([int(v) for v in found], [store.SCHEMA_VERSION])

    def test_the_prefix_is_the_same_in_installer_and_runtime(self) -> None:
        self.assertEqual(PRE_UPGRADE_PREFIX, store.PRE_UPGRADE_PREFIX)
