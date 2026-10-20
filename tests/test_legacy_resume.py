"""Later-boot legacy resume binds unchanged exclusion and original inputs."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest import mock

from install import installer, legacy_state, legacy_transition
from install.context import StepFailedError
from tests.legacy_upgrade_support import BOOT_A, BOOT_B, LegacyCase


class LegacyResumeTests(LegacyCase):
    """Resume the same actual operation through the public installer route."""

    def stage(self) -> None:
        """Establish a real armed exclusion in the disposable installation."""
        installer.install(self.ctx, self.world.repo, self.world.sha)

    def resume(self) -> dict[str, object]:
        """Discover the original run from a new invocation timestamp."""
        later = dataclasses.replace(self.ctx, ts="later-invocation")
        return installer.install(later, self.world.repo, self.world.sha)

    def test_same_boot_refuses_without_releasing_exclusion(self) -> None:
        self.stage()
        with self.assertRaisesRegex(StepFailedError, "restart_required"):
            self.resume()
        self.assertEqual(self.summary()["outcome"], "restart_required")
        self.assertFalse(self.old.exists())
        self.assertFalse(self.ctx.plist.exists())
        self.assertEqual(self.hook().returncode, 0)
        self.assert_source_retained()

    def test_later_boot_preparation_wrapper_keeps_legacy_source_untouched(
        self,
    ) -> None:
        self.stage()
        self.observed_boot.return_value = BOOT_B
        result = legacy_state.read(self.ctx.rdir / "rollback-record.json")
        legacy_transition.verify_legacy_resume(self.ctx, result)
        self.assertEqual(result["ts"], self.ctx.ts)
        self.assertEqual(self.summary()["outcome"], "legacy_cutover_pending")
        self.assertIsNone(self.world.fake.loaded)
        self.assertFalse((self.ctx.lib / self.world.sha / "active").exists())
        self.assertFalse(self.old.exists())
        self.assert_source_retained()
        legacy_transition.verify_legacy_resume(self.ctx, result)
        self.assertEqual(
            legacy_state.read(self.ctx.rdir / "rollback-record.json")[
                "legacy"
            ],
            result["legacy"],
        )

    def test_same_numeric_schema_is_not_an_exact_runtime_inventory(
        self,
    ) -> None:
        (self.old / "muninn/cli.py").write_bytes(b"different legacy runtime")
        with self.assertRaisesRegex(
            StepFailedError, "legacy_runtime_mismatch"
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertFalse(self.ctx.rdir.exists())
        self.assertTrue(self.ctx.plist.exists())
        self.assert_source_retained()

    def test_absent_record_cannot_infer_resume_from_missing_old_root(
        self,
    ) -> None:
        self.stage()
        (self.ctx.rdir / "rollback-record.json").unlink()
        self.observed_boot.return_value = BOOT_B
        with self.assertRaisesRegex(StepFailedError, "legacy_record_missing"):
            self.resume()
        self.assert_source_retained()

    def test_changed_candidate_refuses_before_any_resume_effect(self) -> None:
        self.stage()
        with self.assertRaisesRegex(
            StepFailedError, "legacy_candidate_changed"
        ):
            installer.install(self.ctx, self.world.repo, "f" * 40)
        self.assert_source_retained()

    def test_fence_loss_invalidates_old_baseline_even_after_restoration(
        self,
    ) -> None:
        self.stage()
        self.observed_boot.return_value = BOOT_B
        stub = self.ctx.rdir / "maintenance"
        displaced = stub.with_name("displaced-maintenance")
        stub.rename(displaced)
        with self.assertRaises((OSError, StepFailedError)):
            self.resume()
        displaced.rename(stub)
        with self.assertRaisesRegex(
            StepFailedError, "legacy_recovery_required"
        ):
            self.resume()
        self.assertEqual(self.summary()["outcome"], "recovery_required")
        self.assert_source_retained()

    def replace_source(self, name: str) -> None:
        """Replace exactly one source component while preserving its bytes."""
        self.stage()
        self.observed_boot.return_value = BOOT_B
        path = self.ctx.data / name
        old = path.with_name(name + ".saved")
        path.rename(old)
        path.write_bytes(old.read_bytes())
        path.chmod(0o600)
        with self.assertRaisesRegex(StepFailedError, "legacy_source_changed"):
            self.resume()
        path.unlink()
        old.rename(path)
        self.assert_source_retained()

    def test_database_replacement_refuses(self) -> None:
        self.replace_source("muninn.sqlite")

    def test_key_replacement_refuses(self) -> None:
        self.replace_source("tombstone.key")

    def test_journal_replacement_refuses(self) -> None:
        self.replace_source("tombstones.jsonl")

    def test_malformed_original_boot_cannot_be_treated_as_a_different_boot(
        self,
    ) -> None:
        self.stage()
        record = self.ctx.rdir / "rollback-record.json"
        value = json.loads(record.read_bytes())
        value["legacy"]["boot"] = "darwin:unknown"
        record.write_text(json.dumps(value))
        self.observed_boot.return_value = BOOT_B
        with self.assertRaisesRegex(
            StepFailedError, "legacy_boot_unavailable"
        ):
            self.resume()
        self.assert_source_retained()

    def test_unknown_current_boot_refuses_without_refresh(self) -> None:
        self.stage()
        self.observed_boot.return_value = None
        with self.assertRaisesRegex(
            StepFailedError, "legacy_boot_unavailable"
        ):
            self.resume()
        self.assert_source_retained()

    def test_dry_run_writes_nothing_and_does_not_create_lock(self) -> None:
        before = self.files()
        self.ctx.dry_run = True
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.ctx.lib / ".install-record.lock").exists())
        self.assert_source_retained()

    def files(self) -> dict[Path, tuple[int, bytes | str]]:
        """Record bytes and modes without following fixture links."""
        return {
            p: (
                p.lstat().st_mode,
                str(p.readlink()) if p.is_symlink() else p.read_bytes(),
            )
            for p in self.ctx.home.rglob("*")
            if p.is_symlink() or p.is_file()
        }

    def test_windows_refuses_before_mutating_legacy_paths(self) -> None:
        self.ctx.platform = "win32"
        from_record = {"repo": str(self.world.repo), "sha": self.world.sha}
        before = self.files()
        with (
            mock.patch("install.legacy_transition.preflight"),
            self.assertRaisesRegex(StepFailedError, "legacy_boot_unsupported"),
        ):
            legacy_transition.stage_legacy_exclusion(self.ctx, from_record)
        self.assertEqual(self.files(), before)
        self.assert_source_retained()

    def test_pending_record_is_not_a_serialized_owner(self) -> None:
        self.stage()
        with legacy_state.operation(self.ctx):
            pending = legacy_state.pending(self.ctx)
        self.assertIsNotNone(pending)
        self.assertEqual(
            str((self.ctx.lib / "current").readlink()), self.old.name
        )
        self.assertEqual(BOOT_A, self.observed_boot.return_value)

    def test_pending_dry_run_has_no_filesystem_effects(self) -> None:
        self.stage()
        self.ctx.dry_run = True
        self.observed_boot.return_value = BOOT_B
        before = self.files()
        self.resume()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.summary()["outcome"], "restart_required")
        self.assert_source_retained()

    def test_windows_installer_refuses_selected_legacy_before_native_preflight(
        self,
    ) -> None:
        windows = dataclasses.replace(self.ctx, platform="win32")
        windows.lib.mkdir(parents=True, mode=0o700)
        windows.lib.parent.chmod(0o700)
        (windows.lib / self.old.name).mkdir(mode=0o700)
        manifest = windows.lib / "selection.json"
        manifest.write_text(
            json.dumps({"sha": self.old.name, "python": str(self.python)})
        )
        manifest.chmod(0o600)
        before = self.files()
        with self.assertRaisesRegex(
            StepFailedError, "legacy_boot_unsupported"
        ):
            installer.install(windows, self.world.repo, self.world.sha)
        self.assertEqual(self.files(), before)
        self.assert_source_retained()
