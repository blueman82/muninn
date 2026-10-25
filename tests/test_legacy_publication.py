"""Publication uncertainty preserves actual state for forward recovery."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import (
    installer,
    legacy_cutover,
    legacy_publication,
    record,
    steps_config,
)
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_lock, platform_rebuild, store
from muninn import review_pause_state as state
from tests.legacy_upgrade_support import BOOT_B, LegacyCase


class LegacyPublicationTests(LegacyCase):
    """Inject faults at observable publication and configuration boundaries."""

    def stage(self) -> None:
        """Arm old runtime exclusion and observe a later synthetic boot."""
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.observed_boot.return_value = BOOT_B

    def resume(self) -> dict[str, object]:
        """Discover the same operation from a new invocation."""
        return installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )

    def version(self) -> int:
        """Inspect actual format without treating an exception as rollback."""
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def test_replace_then_raise_preserves_marked_store_and_recovers_forward(
        self,
    ) -> None:
        self.stage()
        original = platform_rebuild.publish_replacement

        def replace_then_raise(home: Path, db: Path, new: Path) -> None:
            original(home, db, new)
            raise OSError("after rename")

        with (
            mock.patch(
                "install.legacy_publication.platform_rebuild.publish_replacement",
                side_effect=replace_then_raise,
            ),
            self.assertRaisesRegex(OSError, "after rename"),
        ):
            self.resume()
        self.assertEqual(self.version(), 1)
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )
        before = self.data_state()["muninn.sqlite"][:2]
        self.resume()
        self.assertEqual(self.data_state()["muninn.sqlite"][:2], before)
        self.assertFalse(self.old.exists())

    def test_record_failure_precedes_replace_and_retry_builds_new_candidate(
        self,
    ) -> None:
        self.stage()
        original = legacy_publication.record.save

        def fail_possible(ctx: Ctx, rec: Record) -> None:
            if rec["legacy"]["phase"] == "publication_possible":
                raise OSError("record unavailable")
            original(ctx, rec)

        with (
            mock.patch(
                "install.legacy_publication.record.save",
                side_effect=fail_possible,
            ),
            self.assertRaisesRegex(OSError, "record unavailable"),
        ):
            self.resume()
        self.assertEqual(self.version(), 3)
        self.assertEqual(self.data_state(), self.original)
        residue = set(self.ctx.data.glob("muninn.sqlite.recovery-*"))
        self.assertEqual(len(residue), 1)
        self.resume()
        self.assertEqual(self.version(), 1)
        self.assertTrue(all(path.exists() for path in residue))

    def test_configuration_failure_never_restores_or_replays_plugin_mutation(
        self,
    ) -> None:
        self.stage()
        with (
            mock.patch(
                "install.legacy_recovery.codex",
                side_effect=OSError("config failure"),
            ),
            self.assertRaisesRegex(OSError, "config failure"),
        ):
            self.resume()
        self.assertEqual(self.version(), 1)
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )
        before = (self.ctx.rdir / "rollback-record.json").read_bytes()
        with self.assertRaisesRegex(
            Exception, "legacy_configuration_incomplete"
        ):
            self.resume()
        self.assertEqual(
            (self.ctx.rdir / "rollback-record.json").read_bytes(), before
        )
        self.assertFalse(self.old.exists())
        self.assertEqual(self.summary()["outcome"], "recovery_required")

    def test_witness_failure_can_resume_without_replacing_again(self) -> None:
        self.stage()
        with (
            mock.patch(
                "install.legacy_publication.state.publish",
                side_effect=OSError("witness failure"),
            ),
            self.assertRaisesRegex(OSError, "witness failure"),
        ):
            self.resume()
        self.assertEqual(self.version(), 1)
        before = self.data_state()["muninn.sqlite"][:2]
        self.resume()
        self.assertEqual(self.data_state()["muninn.sqlite"][:2], before)
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )

    def test_complete_legacy_snapshot_is_idempotent_after_configuration_change(
        self,
    ) -> None:
        self.stage()
        rec = record.load_record(self.ctx.rdir / "rollback-record.json")
        steps_config.record(self.ctx, rec)
        before = (self.ctx.rdir / "rollback-record.json").read_bytes()
        self.ctx.config.write_text('model = "changed-after-snapshot"\n')
        steps_config.record(self.ctx, rec)
        self.assertEqual(
            (self.ctx.rdir / "rollback-record.json").read_bytes(), before
        )

    def test_interrupted_cache_snapshot_refuses_to_replace_partial_backup(
        self,
    ) -> None:
        self.stage()
        rec = record.load_record(self.ctx.rdir / "rollback-record.json")
        rec["has_codex"] = True
        self.ctx.config.write_text('model = "original"\n')
        self.ctx.cache.mkdir(parents=True)
        (self.ctx.cache / "retained").write_bytes(b"original")
        backup = self.ctx.rdir / "codex-cache"

        def interrupt_copy(source: Path, target: Path) -> None:
            self.assertEqual(source, self.ctx.cache)
            target.mkdir()
            (target / "partial").write_bytes(b"incomplete")
            raise OSError("snapshot interrupted")

        with (
            mock.patch("install.steps_config.shutil.copytree", interrupt_copy),
            self.assertRaisesRegex(OSError, "snapshot interrupted"),
        ):
            steps_config.record(self.ctx, rec)
        self.assertNotIn("config_snapshot_complete", rec)
        with self.assertRaises(FileExistsError):
            steps_config.record(self.ctx, rec)
        self.assertEqual((backup / "partial").read_bytes(), b"incomplete")
        self.assertEqual(
            (self.ctx.cache / "retained").read_bytes(), b"original"
        )

    def check_archive_gap_substitution(self, kind: str) -> None:
        """Refuse authority loss before exposing any candidate database."""
        self.stage()
        original = legacy_cutover.platform_rebuild.retain_upgrade_store

        def archive(home: Path, source: Path) -> Path:
            result = original(home, source)
            if kind == "lock":
                lock = self.ctx.lib / ".install-record.lock"
                lock.rename(lock.with_name("displaced-operation-lock"))
                os.close(platform_lock.acquire(lock))
            elif kind == "fence":
                self.ctx.muninn.unlink()
                self.ctx.muninn.symlink_to(self.ctx.lib / "current/bin/muninn")
            else:
                (self.ctx.lib / self.world.sha / "bin/muninn").write_bytes(
                    b"changed candidate runtime"
                )
            return result

        with (
            mock.patch.object(
                legacy_cutover.platform_rebuild,
                "retain_upgrade_store",
                archive,
            ),
            self.assertRaisesRegex(
                StepFailedError,
                {
                    "lock": "install_lock_changed",
                    "fence": "legacy_exclusion_changed",
                    "runtime": "legacy_installation_changed",
                }[kind],
            ),
        ):
            self.resume()
        self.assertEqual(self.data_state(), self.original)
        self.assertFalse((self.ctx.data / state.WITNESS).exists())
        self.assertFalse(self.world.fake.loaded)

    def test_operation_lock_substitution_after_archive_blocks_publication(
        self,
    ) -> None:
        self.check_archive_gap_substitution("lock")

    def test_launcher_fence_substitution_after_archive_blocks_publication(
        self,
    ) -> None:
        self.check_archive_gap_substitution("fence")

    def test_prepared_runtime_substitution_after_archive_blocks_publication(
        self,
    ) -> None:
        self.check_archive_gap_substitution("runtime")
