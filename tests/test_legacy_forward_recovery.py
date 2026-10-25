"""Successor states retain fences and recheck data after configuration."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import (
    installer,
    legacy_publication,
    legacy_recovery,
    legacy_state,
)
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_lock, store, store_fingerprint
from muninn import review_pause as q
from muninn import review_pause_state as state
from muninn.tombstone_key import TombstoneKeyError
from tests.legacy_cutover_support import crash
from tests.legacy_upgrade_support import BOOT_B, LegacyCase
from tests.store_support import insert_event, insert_source
from tests.test_review_shared_models import policy_data


class ForwardRecoveryTests(LegacyCase):
    """Exercise final replay and refusal boundaries with real SQLite state."""

    def stage(self) -> None:
        """Arm the retained operation and observe a later synthetic boot."""
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.observed_boot.return_value = BOOT_B

    def resume(self) -> Record:
        """Resume through public discovery with the same source checkout."""
        return installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )

    def check_recovery_authority_loss(self, kind: str) -> None:
        """Refuse publication proof when recovery loses authority."""
        self.stage()
        original_replace = (
            legacy_publication.platform_rebuild.publish_replacement
        )

        def replace_then_raise(home: Path, db: Path, new: Path) -> None:
            original_replace(home, db, new)
            raise OSError("after rename")

        with (
            mock.patch(
                "install.legacy_publication.platform_rebuild.publish_replacement",
                side_effect=replace_then_raise,
            ),
            self.assertRaisesRegex(OSError, "after rename"),
        ):
            self.resume()
        witness = self.ctx.data / state.WITNESS
        self.assertFalse(witness.exists())
        before = self.data_state()
        original_prepare = legacy_recovery.prepare
        original_capture = store_fingerprint.capture
        changed = False

        def capture(home: Path) -> store_fingerprint.Fingerprint:
            nonlocal changed
            result = original_capture(home)
            if not changed:
                changed = True
                if kind == "lock":
                    path = self.ctx.lib / ".install-record.lock"
                    path.rename(path.with_name("original-recovery-lock"))
                    os.close(platform_lock.acquire(path))
                else:
                    path = self.ctx.lib / self.world.sha / "bin/muninn"
                    path.write_bytes(path.read_bytes() + b"\n# replaced\n")
            return result

        def prepare(
            ctx: Ctx,
            rec: Record,
            owner: q.MaintenanceOwner,
            check: legacy_state.OperationCheck,
        ) -> Path:
            with mock.patch(
                "install.legacy_recovery.store_fingerprint.capture",
                side_effect=capture,
            ):
                return original_prepare(ctx, rec, owner, check)

        error = (
            "install_lock_changed"
            if kind == "lock"
            else "legacy_installation_changed"
        )
        with (
            mock.patch("install.legacy_recovery.prepare", side_effect=prepare),
            self.assertRaisesRegex(StepFailedError, error),
        ):
            self.resume()
        self.assertTrue(changed)
        self.assertFalse(witness.exists())
        rec = legacy_state.read(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(rec["legacy"]["phase"], "publication_possible")
        self.assertEqual(rec["legacy"]["publication"], "possible")
        self.assertEqual(self.data_state(), before)
        self.assertFalse(self.world.fake.loaded)
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )

    def test_recovery_lock_loss_cannot_publish_witness(self) -> None:
        self.check_recovery_authority_loss("lock")

    def test_recovery_runtime_loss_cannot_publish_witness(self) -> None:
        self.check_recovery_authority_loss("runtime")

    def test_final_writer_replays_new_intent_and_snapshots_latest_boundaries(
        self,
    ) -> None:
        policy = policy_data()
        policy["projection_version"] = "permitted-fields-v2"
        self.ctx.review_policy = self.ctx.home / "policy.json"
        self.ctx.review_policy.write_text(json.dumps(policy))
        self.stage()
        original = legacy_recovery.configure

        def configure(ctx: Ctx, rec: Record) -> None:
            original(ctx, rec)
            with (
                self.assertRaises(BlockingIOError),
                legacy_state.operation(ctx),
            ):
                self.fail("operation lock was released")
            with (
                self.assertRaisesRegex(ValueError, "coordination_timeout"),
                q.maintenance_owner(
                    ctx.data, wait_s=0, status=q.EraseStatus()
                ),
            ):
                self.fail("owner was released")
            with (
                store.writer_lock(ctx.data, wait_s=0),
                closing(store.connect_rw(store.db_path(ctx.data))) as conn,
            ):
                source = insert_source(conn)
                insert_event(conn, source, 1, text="late erased row")
                conn.execute(
                    "UPDATE source SET cursor_line=31,anchor_sha256='boundary'"
                )
            (ctx.data / "tombstones.jsonl").write_text(
                json.dumps(
                    {"provider": "codex", "level": "thread", "thread_id": "t1"}
                )
                + "\n"
            )

        with mock.patch(
            "install.legacy_recovery.configure", side_effect=configure
        ):
            result = self.resume()
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_source").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(result["policy_status"], "active")

    def test_live_path_replacement_in_configuration_gap_refuses_activation(
        self,
    ) -> None:
        self.stage()
        original = legacy_recovery.configure

        def configure(ctx: Ctx, rec: Record) -> None:
            original(ctx, rec)
            path = store.db_path(ctx.data)
            saved = path.with_suffix(".displaced")
            path.rename(saved)
            path.write_bytes(saved.read_bytes())
            path.chmod(0o600)

        with (
            mock.patch(
                "install.legacy_recovery.configure", side_effect=configure
            ),
            self.assertRaisesRegex(
                StepFailedError, "review_activation_store_changed"
            ),
        ):
            self.resume()
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )
        self.assertFalse(self.old.exists())

    def test_missing_witness_after_compatible_exposure_cannot_use_old_boot(
        self,
    ) -> None:
        self.stage()
        done = crash(
            self.ctx, self.world.repo, self.world.sha, "after_configure"
        )
        self.assertEqual(done.returncode, 91, done.stderr.decode())
        witness = self.ctx.data / state.WITNESS
        witness.unlink()
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            self.resume()
        self.assertFalse(witness.exists())
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )

    def fail_before_publication(self, function: str) -> None:
        """Require failed preparation to preserve original bytes."""
        self.stage()
        with (
            mock.patch(function, side_effect=OSError("injected fault")),
            self.assertRaisesRegex(OSError, "injected fault"),
        ):
            self.resume()
        self.assertEqual(self.data_state(), self.original)
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )
        self.assertFalse((self.ctx.data / state.WITNESS).exists())

    def test_archive_sync_failure_blocks_publication(self) -> None:
        self.fail_before_publication(
            "install.legacy_cutover.platform_rebuild._sync_parent"
        )
        self.assertEqual(
            len(list(self.ctx.data.glob("muninn.sqlite.pre-upgrade-*"))), 1
        )

    def test_copy_failure_blocks_publication(self) -> None:
        self.fail_before_publication(
            "install.legacy_cutover.store_copy.copy_governed_state"
        )

    def test_replay_cleanup_failure_blocks_publication(self) -> None:
        self.fail_before_publication(
            "install.legacy_cutover.tombstones.reapply_tombstones"
        )

    def test_candidate_sync_failure_blocks_publication(self) -> None:
        original = legacy_recovery.file_sync.sync_path

        def sync(path: Path) -> None:
            if path.name.startswith(store.RECOVERY_PREFIX):
                raise OSError("injected fault")
            original(path)

        self.stage()
        with (
            mock.patch(
                "install.legacy_cutover.file_sync.sync_path", side_effect=sync
            ),
            self.assertRaisesRegex(OSError, "injected fault"),
        ):
            self.resume()
        self.assertEqual(self.data_state(), self.original)

    def test_required_key_missing_refuses_before_archive(self) -> None:
        journal = self.ctx.data / "tombstones.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "provider": "codex",
                    "level": "line",
                    "thread_id": "t1",
                    "line": 0,
                    "line_sha256": "ev2:" + "a" * 64,
                }
            )
            + "\n"
        )
        (self.ctx.data / "tombstone.key").unlink()
        self.stage()
        with self.assertRaises(TombstoneKeyError):
            self.resume()
        self.assertFalse(
            list(self.ctx.data.glob("muninn.sqlite.pre-upgrade-*"))
        )
        self.assertEqual(
            (self.ctx.data / "muninn.sqlite").read_bytes(),
            self.original["muninn.sqlite"][2],
        )
        self.assertFalse((self.ctx.data / "tombstone.key").exists())

    def test_native_stop_and_subprocesses_never_run_under_writer(self) -> None:
        original = self.ctx.run

        def run(
            argv: Sequence[str | Path],
            env: Mapping[str, str] | None = None,
            input: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            with store.writer_lock(self.ctx.data, wait_s=0):
                pass
            if list(map(str, argv[:2])) == ["launchctl", "bootout"]:
                self.assertTrue(platform_lock.released(self.ctx.data))
            return original(argv, env, input)

        self.ctx.run = run
        self.stage()
        self.resume()

    def test_key_substitution_during_configuration_refuses_before_replay(
        self,
    ) -> None:
        self.stage()
        original = legacy_recovery.configure

        def configure(ctx: Ctx, rec: Record) -> None:
            original(ctx, rec)
            key = ctx.data / "tombstone.key"
            key.rename(key.with_suffix(".retained"))
            key.write_bytes(b"z" * 32)
            key.chmod(0o600)

        with (
            mock.patch(
                "install.legacy_recovery.configure", side_effect=configure
            ),
            self.assertRaisesRegex(StepFailedError, "legacy_key_changed"),
        ):
            self.resume()
