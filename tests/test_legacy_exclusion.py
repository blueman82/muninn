"""Legacy upgrades exclude managed launches before asking for a restart."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from unittest import mock

from install import installer, legacy_exclusion
from install.context import StepFailedError
from install.lifecycle import linux_unit
from install.record import save
from tests.legacy_upgrade_support import BOOT_B, LegacyCase, LinuxService


class LegacyExclusionTests(LegacyCase):
    """Use the real installer with an exact old release and native fakes."""

    def test_upgrade_excludes_old_launches_without_touching_source(
        self,
    ) -> None:
        rec = installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(self.summary()["outcome"], "restart_required")
        self.assertEqual(rec["legacy"]["phase"], "armed")
        self.assertFalse(self.old.exists())
        self.assertFalse(self.ctx.plist.exists())
        self.assertIsNone(self.world.fake.loaded)
        self.assertNotIn("pin", rec["steps"])
        self.assertFalse(
            {"claude", "codex", "cursor", "restart", "prepare_store"}
            & set(rec["steps"])
        )
        hook = self.hook()
        self.assertEqual(hook.returncode, 0)
        self.assertEqual(
            json.loads(hook.stdout),
            {"error": "maintenance", "reason": "restart_required"},
        )
        self.assertEqual(hook.stderr, b"")
        self.assert_source_retained()

    def test_registered_idle_linux_unit_is_disabled_stopped_and_removed(
        self,
    ) -> None:
        self.ctx.plist.unlink()
        self.ctx = dataclasses.replace(self.ctx, platform="linux")
        self.ctx.plist.parent.mkdir(parents=True)
        self.ctx.plist.write_text(
            linux_unit(self.ctx, self.python, self.ctx.lib / "current")
        )
        self.ctx.plist.chmod(0o600)
        service = LinuxService(self)
        self.ctx.run = service
        self.observed_boot.return_value = "linux:" + BOOT_B.split(":")[1]
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertFalse(service.enabled)
        self.assertFalse(service.registered)
        self.assertIn("disable", service.calls)
        self.assertIn("stop", service.calls)
        self.assertIn("daemon-reload", service.calls)
        self.assertFalse(self.ctx.plist.exists())
        self.assert_source_retained()

    def test_captured_old_root_cannot_open_modules_after_exclusion(
        self,
    ) -> None:
        captured = self.ctx.muninn.resolve().parent.parent
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(captured, self.old)
        for relative in ("bin/muninn", "muninn/cli.py", "muninn/store.py"):
            with self.assertRaises(FileNotFoundError):
                (captured / relative).open("rb")
        self.assertTrue(
            (legacy_exclusion.retained(self.ctx) / "muninn/cli.py").is_file()
        )
        result = subprocess.run(
            [self.ctx.muninn, "ingest"], capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"], "maintenance")
        self.assert_source_retained()

    def test_initial_unknown_boot_refuses_before_any_exclusion(self) -> None:
        self.observed_boot.return_value = "unknown"
        with self.assertRaisesRegex(
            StepFailedError, "legacy_boot_unavailable"
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertFalse(self.ctx.rdir.exists())
        self.assertTrue(self.old.exists())
        self.assertTrue(self.ctx.plist.exists())
        self.assert_source_retained()

    def test_boot_is_recorded_only_after_persistent_exclusion(self) -> None:
        observed: list[str] = []
        real_save = save

        def inspect(ctx: installer.Ctx, rec: installer.Record) -> None:
            phase = rec["legacy"]["phase"]
            if "boot" in rec["legacy"]:
                self.assertIn(phase, ("armed", "recovery_required"))
                self.assertFalse(self.old.exists())
                self.assertFalse(self.ctx.plist.exists())
                self.assertIsNone(self.world.fake.loaded)
                observed.append(phase)
            real_save(ctx, rec)

        with mock.patch("install.legacy_state.save", side_effect=inspect):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(observed, ["armed", "armed"])
        self.assert_source_retained()

    def test_configured_provider_commands_and_cache_remain_exact(self) -> None:
        fragment = self.old / "integrations/claude/settings-hooks.json"
        self.ctx.settings.write_bytes(fragment.read_bytes())
        self.ctx.settings.chmod(0o600)
        cursor = self.old / "integrations/cursor/hooks.json"
        self.ctx.cursor_settings.parent.mkdir(parents=True, exist_ok=True)
        self.ctx.cursor_settings.write_bytes(cursor.read_bytes())
        self.ctx.cursor_settings.chmod(0o600)
        self.ctx.config.write_text(
            '[marketplaces.muninn-local]\nsource_type="local"\nsource='
            + json.dumps(str(self.ctx.lib / "current/integrations/codex"))
            + '\n[plugins."muninn@muninn-local"]\nenabled=true\n'
        )
        self.ctx.config.chmod(0o600)
        shutil.copytree(
            self.old / "integrations/codex", self.ctx.cache / "0.2.0"
        )
        original = {
            path: path.read_bytes()
            for path in (
                self.ctx.settings,
                self.ctx.config,
                self.ctx.cursor_settings,
            )
        }
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual({p: p.read_bytes() for p in original}, original)
        self.assertEqual(self.hook().returncode, 0)
        self.assert_source_retained()

    def test_partial_managed_provider_fragment_refuses_before_exclusion(
        self,
    ) -> None:
        fragment = json.loads(
            (self.old / "integrations/claude/settings-hooks.json").read_bytes()
        )
        fragment["hooks"].pop("UserPromptSubmit")
        self.ctx.settings.write_text(json.dumps(fragment))
        self.ctx.settings.chmod(0o600)
        with self.assertRaisesRegex(
            StepFailedError, "legacy_provider_mismatch"
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertFalse(self.ctx.rdir.exists())
        self.assertTrue(self.old.exists())
        self.assert_source_retained()
