"""Installer serialization and partial legacy exclusion fail closed."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import installer, legacy_exclusion, legacy_state, rollback
from install.context import StepFailedError
from install.record import Record, save
from muninn import platform_lock
from tests.legacy_upgrade_support import BOOT_B, LegacyCase


class LegacyRecoveryTests(LegacyCase):
    """Inject faults around real private filesystem and lock transitions."""

    def test_intent_write_failure_does_not_change_any_launch_path(
        self,
    ) -> None:
        with (
            mock.patch(
                "install.legacy_state.save", side_effect=OSError("intent")
            ),
            self.assertRaisesRegex(OSError, "intent"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertTrue(self.old.is_dir())
        self.assertTrue(self.ctx.plist.is_file())
        self.assertEqual(self.ctx.muninn.resolve(), self.old / "bin/muninn")
        self.assertNotIn(
            "prepare_release", [c[0] for c in self.world.fake.calls]
        )
        self.assert_source_retained()

    def test_operation_lock_blocks_distinct_timestamp_initial_attempt(
        self,
    ) -> None:
        other = dataclasses.replace(self.ctx, ts="other-first-run")
        path = self.ctx.lib / ".install-record.lock"
        with legacy_state.operation(self.ctx):
            inode = path.stat().st_ino
            with self.assertRaises(BlockingIOError):
                installer.install(other, self.world.repo, self.world.sha)
            self.assertEqual(path.stat().st_ino, inode)
        self.assertFalse(other.rdir.exists())
        self.assertFalse(self.ctx.rdir.exists())
        self.assert_source_retained()

    def test_darwin_gap_keeps_operation_lock_but_releases_owner_and_writer(
        self,
    ) -> None:
        run = self.ctx.run
        seen: list[str] = []

        def inspect(
            argv: Sequence[str | Path],
            env: Mapping[str, str] | None = None,
            input: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            fd = platform_lock.acquire(self.ctx.data / "writer.lock")
            os.close(fd)
            if list(argv[:2]) == ["launchctl", "bootout"]:
                owner = platform_lock.acquire(
                    self.ctx.data / "review.supervisor.lock"
                )
                os.close(owner)
                contender = dataclasses.replace(self.ctx, ts="gap-contender")
                with self.assertRaises(BlockingIOError):
                    installer.install(
                        contender, self.world.repo, self.world.sha
                    )
                rec = json.loads(
                    (self.ctx.rdir / "rollback-record.json").read_bytes()
                )
                with self.assertRaises(BlockingIOError):
                    rollback.rollback(self.ctx, rec)
                self.assertFalse(contender.rdir.exists())
                seen.append("gap")
            return run(argv, env, input)

        self.ctx.run = inspect
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(seen, ["gap"])
        self.assert_source_retained()

    def test_substituted_operation_lock_refuses_without_unlinking_it(
        self,
    ) -> None:
        path = self.ctx.lib / ".install-record.lock"
        with (
            self.assertRaisesRegex(StepFailedError, "install_lock_changed"),
            legacy_state.operation(self.ctx) as check,
        ):
            path.rename(path.with_name("saved-lock"))
            path.touch(mode=0o600)
            check()
        self.assertTrue(path.exists())
        self.assertTrue(path.with_name("saved-lock").exists())
        self.assert_source_retained()

    def test_child_crash_releases_operation_descriptor_not_record_authority(
        self,
    ) -> None:
        code = (
            "import os,sys; from pathlib import Path; "
            "from install.context import Ctx,run_real; "
            "from install.legacy_state import operation; "
            "c=Ctx(Path(sys.argv[1]),run_real,'child',platform='darwin'); "
            "guard=operation(c); guard.__enter__(); os._exit(71)"
        )
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.ctx.home)], check=False
        )
        self.assertEqual(child.returncode, 71)
        with legacy_state.operation(self.ctx):
            self.assertIsNone(legacy_state.pending(self.ctx))
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assert_source_retained()

    def fail_phase(self, target: str) -> None:
        """Interrupt after one persisted intent or completed effect."""
        phase = legacy_state.phase
        cut_once: list[str] = []

        def cut(ctx: installer.Ctx, rec: Record, name: str) -> None:
            phase(ctx, rec, name)
            if name == target and not cut_once:
                cut_once.append(name)
                raise OSError(target)

        with (
            mock.patch("install.legacy_state.phase", side_effect=cut),
            self.assertRaisesRegex(OSError, target),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(self.summary()["outcome"], "recovery_required")
        if target != "launcher_intent":
            self.assertIsNone(self.world.fake.loaded)
            self.assertFalse(self.ctx.plist.exists())
        self.observed_boot.return_value = BOOT_B
        with self.assertRaisesRegex(
            StepFailedError, "legacy_recovery_required"
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        rec = json.loads((self.ctx.rdir / "rollback-record.json").read_bytes())
        self.assertTrue(rollback.rollback(self.ctx, rec))
        self.assert_source_retained()
        self.assertFalse(
            any(
                c[:2]
                in (["launchctl", "bootstrap"], ["launchctl", "kickstart"])
                for c in self.world.fake.calls
            )
        )

    def test_cut_after_launcher_intent(self) -> None:
        self.fail_phase("launcher_intent")

    def test_cut_after_terminal_launcher(self) -> None:
        self.fail_phase("launcher")

    def test_cut_after_runtime_move_intent(self) -> None:
        self.fail_phase("runtime_intent")

    def test_cut_after_runtime_move(self) -> None:
        self.fail_phase("runtime")

    def test_cut_before_native_stop(self) -> None:
        self.fail_phase("service_intent")

    def test_cut_after_persistent_service_removal(self) -> None:
        self.fail_phase("service")

    def test_cut_after_armed_record_never_reuses_boot_baseline(self) -> None:
        self.fail_phase("armed")

    def test_future_possible_publication_record_never_activates_old_runtime(
        self,
    ) -> None:
        rec = installer.install(self.ctx, self.world.repo, self.world.sha)
        rec["legacy"]["publication"] = "possible"
        save(self.ctx, rec)
        with self.assertRaisesRegex(StepFailedError, "legacy_record_invalid"):
            rollback.rollback(self.ctx, rec)
        self.assertIsNone(self.world.fake.loaded)
        self.assertFalse(self.ctx.plist.exists())
        self.assertFalse(self.old.exists())
        self.assert_source_retained()

    def test_real_process_crash_leaves_pending_partial_stage_unarmed(
        self,
    ) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        child = os.fork()
        if child == 0:

            def crash(ctx: installer.Ctx, rec: Record, name: str) -> None:
                if name == "runtime":
                    os._exit(72)
                original_phase(ctx, rec, name)

            original_phase = legacy_state.phase
            with mock.patch("install.legacy_state.phase", side_effect=crash):
                installer.install(self.ctx, self.world.repo, self.world.sha)
            os._exit(73)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 72)
        self.observed_boot.return_value = BOOT_B
        with self.assertRaisesRegex(
            StepFailedError, "legacy_recovery_required"
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        record = json.loads(
            (self.ctx.rdir / "rollback-record.json").read_bytes()
        )
        self.assertEqual(record["legacy"]["phase"], "runtime_intent")
        self.assertNotIn("boot", record["legacy"])
        self.assertFalse(self.old.exists())
        self.assert_source_retained()

    def test_missing_legacy_marker_cannot_fall_through_to_normal_rollback(
        self,
    ) -> None:
        rec = installer.install(self.ctx, self.world.repo, self.world.sha)
        del rec["legacy"]
        save(self.ctx, rec)
        with self.assertRaisesRegex(StepFailedError, "legacy_record_invalid"):
            rollback.rollback(self.ctx, rec)
        self.assertIsNone(self.world.fake.loaded)
        self.assert_source_retained()

    def test_directory_sync_failure_after_move_retains_uncertainty(
        self,
    ) -> None:
        sync = legacy_exclusion.file_sync.sync_path
        failed: list[bool] = []

        def fail(path: Path) -> None:
            if path == self.ctx.lib and not self.old.exists() and not failed:
                failed.append(True)
                raise OSError("move_directory_sync")
            sync(path)

        with (
            mock.patch(
                "install.legacy_exclusion.file_sync.sync_path",
                side_effect=fail,
            ),
            self.assertRaisesRegex(OSError, "move_directory_sync"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(self.summary()["outcome"], "recovery_required")
        self.assertIsNone(self.world.fake.loaded)
        self.assertFalse(self.ctx.plist.exists())
        self.assert_source_retained()
