"""Rehearse lifecycle hook reconciliation and trust in temporary homes."""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from install import installer as co
from install import rollback as rb
from install import steps_config, uninstall
from install.context import Ctx, StepFailedError
from tests.installer_support import World, dump, git

EVENTS = {
    "SessionStart": "session-start",
    "SessionEnd": "session-end",
    "PreCompact": "pre-compact",
    "UserPromptSubmit": "prompt",
}
CURSOR_EVENTS = {
    "sessionStart": "session-start",
    "sessionEnd": "session-end",
    "preCompact": "pre-compact",
}


class InstallLearningHookTests(unittest.TestCase):
    """Installer merges and restores complete owned native hook sets."""

    def setUp(self) -> None:
        self.w = World(self)

    def install(self) -> dict[str, Any]:
        """Install through the existing isolated runner and service fakes."""
        return co.install(
            self.w.ctx(fresh=True, cursor_hooks=True), self.w.repo, self.w.sha
        )

    def upgrade(self) -> tuple[Ctx, str]:
        """Prepare a new fake release and an independent upgrade context."""
        (self.w.repo / "bin/note").write_text("next release\n")
        git(self.w.repo, "add", "-A")
        git(self.w.repo, "commit", "-qm", "next")
        sha = git(self.w.repo, "rev-parse", "HEAD").decode().strip()
        ctx = dataclasses.replace(
            self.w.ctx(upgrade=True, cursor_hooks=True),
            ts="20261002T000000Z",
        )
        return ctx, sha

    def before_store_preparation(self) -> tuple[Ctx, dict[str, Any]]:
        """Capture real config/cache before any store or activation work."""
        ctx, sha = self.upgrade()
        rec = co.preflight(ctx, self.w.repo, sha)
        co.record(ctx, rec)
        self.assertNotIn("store", rec)
        self.assertFalse(rec.get("quiesced", False))
        return ctx, rec

    def assert_hooks(self) -> None:
        """Assert installed provider events and literal boundary commands."""
        ctx = self.w.ctx()
        prefix = f"{ctx.muninn} hook "
        for provider, path in (
            ("claude", ctx.settings),
            ("codex", ctx.release / "integrations/codex/hooks/hooks.json"),
        ):
            hooks = json.loads(path.read_bytes())["hooks"]
            for event, verb in EVENTS.items():
                with self.subTest(provider=provider, event=event):
                    self.assertIn(event, hooks)
                    matching = [
                        group
                        for group in hooks.get(event, [])
                        if any(
                            h.get("command")
                            == prefix + verb + f" --provider {provider}"
                            for h in group["hooks"]
                        )
                    ]
                    self.assertEqual(len(matching), 1)
                    if event == "SessionStart":
                        self.assertEqual(
                            matching[0]["matcher"],
                            "startup|resume|clear|compact",
                        )
        hooks = json.loads(ctx.cursor_settings.read_bytes())["hooks"]
        for event, verb in CURSOR_EVENTS.items():
            with self.subTest(provider="cursor", event=event):
                self.assertIn(event, hooks)
                matching = [
                    h
                    for h in hooks.get(event, [])
                    if h.get("command") == prefix + verb + " --provider cursor"
                ]
                self.assertEqual(len(matching), 1)

    def test_upgrade_reconciles_all_managed_hooks(self) -> None:
        self.install()
        ctx = self.w.ctx()
        for path in (ctx.settings, ctx.cursor_settings):
            doc = json.loads(path.read_bytes())
            retained = (
                "UserPromptSubmit" if path == ctx.settings else "preCompact"
            )
            doc["hooks"] = {retained: doc["hooks"][retained]}
            path.write_bytes(dump(doc))
        ctx.config.write_text(co.drop_trust(ctx.config.read_text()))
        upgrade, sha = self.upgrade()
        rec = co.install(upgrade, self.w.repo, sha)
        self.assert_hooks()
        self.assertTrue(rec["has_claude"])
        self.assertTrue(rec["has_codex"])
        self.assertEqual(len(rec["codex_hooks"]), 4)
        self.assertTrue((ctx.data / "recall.off").exists())
        self.assertFalse(
            any("import-cursor" in call for call in self.w.fake.calls)
        )

    def test_changed_hash_requires_trust(self) -> None:
        self.install()
        ctx = self.w.ctx()
        old = ctx.config.read_text()
        self.w.fake.version = b"codex-cli 0.162.1\n"
        pinned = self.w.repo / "integrations/codex/hooks/hooks.json"
        doc = json.loads(pinned.read_bytes())
        doc["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 6
        pinned.write_bytes(dump(doc))
        upgrade, sha = self.upgrade()
        rec = co.install(upgrade, self.w.repo, sha)
        self.assertEqual(rec["trust"], "owner")
        self.assertNotEqual(ctx.config.read_text(), old)
        self.assertNotIn("trusted_hash", ctx.config.read_text())
        self.assertTrue(
            all(
                value["trust"] == "untrusted"
                for value in rec["codex_hooks"].values()
            )
        )

    def test_unrelated_hooks_preserved(self) -> None:
        ctx = self.w.ctx()
        foreign = {"type": "command", "command": "other-tool"}
        managed = {
            "type": "command",
            "command": f"{ctx.muninn} hook session-start --provider claude",
        }
        doc = json.loads(ctx.settings.read_bytes())
        doc["hooks"] = {
            "SessionStart": [
                {"matcher": "resume", "hooks": [managed, foreign]}
            ],
            "Stop": [{"hooks": [foreign]}],
        }
        ctx.settings.write_bytes(dump(doc))
        cursor_doc = {
            "version": 1,
            "other": True,
            "hooks": {
                event: [{"command": "other-tool"}] for event in CURSOR_EVENTS
            },
        }
        ctx.cursor_settings.parent.mkdir()
        ctx.cursor_settings.write_bytes(dump(cursor_doc))
        self.install()
        self.assert_hooks()
        result = json.loads(ctx.settings.read_bytes())
        self.assertEqual(
            result["hooks"]["SessionStart"][0],
            {"matcher": "resume", "hooks": [foreign]},
        )
        self.assertEqual(result["hooks"]["Stop"], [{"hooks": [foreign]}])
        cursor = json.loads(ctx.cursor_settings.read_bytes())
        self.assertTrue(cursor["other"])
        for event in CURSOR_EVENTS:
            self.assertEqual(
                cursor["hooks"][event][0], {"command": "other-tool"}
            )
        unhooked = json.loads(uninstall.drop_hooks(ctx.settings.read_bytes()))
        self.assertEqual(
            unhooked["hooks"]["SessionStart"],
            [{"matcher": "resume", "hooks": [foreign]}],
        )

    def test_failed_upgrade_retains_current_config_and_trust(self) -> None:
        self.install()
        ctx = self.w.ctx()
        cursor = json.loads(ctx.cursor_settings.read_bytes())
        for event in CURSOR_EVENTS:
            cursor["hooks"].setdefault(event, []).insert(
                0, {"command": "before"}
            )
        ctx.cursor_settings.write_bytes(dump(cursor))
        before = {
            path: path.read_bytes()
            for path in (ctx.settings, ctx.config, ctx.cursor_settings)
        }
        upgrade, sha = self.upgrade()
        self.w.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            co.install(upgrade, self.w.repo, sha)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data, path.name)
        self.assertEqual((ctx.lib / "current").readlink(), Path(sha))
        self.assertIsNone(self.w.fake.loaded)

    def test_cursor_rollback_preserves_new_foreign_handlers(self) -> None:
        rec = self.install()
        ctx = self.w.ctx()
        doc = json.loads(ctx.cursor_settings.read_bytes())
        for event in CURSOR_EVENTS:
            doc["hooks"].setdefault(event, []).append(
                {"command": "new-foreign"}
            )
        ctx.cursor_settings.write_bytes(dump(doc))
        rb.rollback(ctx, rec)
        result = json.loads(ctx.cursor_settings.read_bytes())
        self.assertEqual(
            result["hooks"],
            {event: [{"command": "new-foreign"}] for event in CURSOR_EVENTS},
        )

    def test_managed_cursor_hooks_reconcile_without_new_opt_in(self) -> None:
        self.install()
        ctx, sha = self.upgrade()
        ctx.cursor_hooks = False
        doc = json.loads(ctx.cursor_settings.read_bytes())
        doc["hooks"].pop("sessionStart", None)
        ctx.cursor_settings.write_bytes(dump(doc))
        co.install(ctx, self.w.repo, sha)
        self.assert_hooks()

    def test_foreign_cursor_hooks_stay_manual_on_upgrade(self) -> None:
        ctx = self.w.ctx()
        ctx.cursor_settings.parent.mkdir()
        ctx.cursor_settings.write_bytes(
            dump(
                {
                    "version": 1,
                    "hooks": {"preCompact": [{"command": "another-tool"}]},
                }
            )
        )
        before = ctx.cursor_settings.read_bytes()
        co.install(self.w.ctx(fresh=True), self.w.repo, self.w.sha)
        ctx, sha = self.upgrade()
        ctx.cursor_hooks = False
        co.install(ctx, self.w.repo, sha)
        self.assertEqual(ctx.cursor_settings.read_bytes(), before)

    def test_verify_runs_all_owned_cursor_commands(self) -> None:
        self.install()
        ctx = self.w.ctx()
        for verb in ("session-start", "session-end", "pre-compact"):
            self.assertIn(
                [str(ctx.muninn), "hook", verb, "--provider", "cursor"],
                self.w.fake.calls,
            )

    def test_install_record_names_every_managed_cursor_event(self) -> None:
        self.install()
        ctx = self.w.ctx()
        result = json.loads((ctx.lib / co.INSTALL_RECORD).read_bytes())
        self.assertLessEqual(
            {"hooks.sessionStart", "hooks.sessionEnd", "hooks.preCompact"},
            set(result["config_keys"]),
        )

    def test_uninstall_removes_all_owned_cursor_events(self) -> None:
        self.install()
        ctx = self.w.ctx()
        doc = json.loads(ctx.cursor_settings.read_bytes())
        for handlers in doc["hooks"].values():
            handlers.insert(0, {"command": "keep-foreign"})
        ctx.cursor_settings.write_bytes(dump(doc))
        uninstall._unhook_cursor(ctx)
        result = json.loads(ctx.cursor_settings.read_bytes())
        self.assertEqual(
            result["hooks"],
            {event: [{"command": "keep-foreign"}] for event in CURSOR_EVENTS},
        )

    def test_failed_upgrade_retains_new_cache_and_prior_snapshot(self) -> None:
        self.install()
        ctx = self.w.ctx()
        cache = ctx.cache / "0.2.0/hooks/hooks.json"
        before = cache.read_bytes()
        pinned = self.w.repo / "integrations/codex/hooks/hooks.json"
        doc = json.loads(pinned.read_bytes())
        doc["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 6
        pinned.write_bytes(dump(doc))
        upgrade, sha = self.upgrade()
        self.w.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            co.install(upgrade, self.w.repo, sha)
        self.assertNotEqual(cache.read_bytes(), before)
        self.assertEqual(
            (upgrade.rdir / "codex-cache/0.2.0/hooks/hooks.json").read_bytes(),
            before,
        )
        self.assertIsNone(self.w.fake.loaded)

    def test_failed_fresh_install_restores_absent_cache(self) -> None:
        ctx = self.w.ctx(fresh=True, cursor_hooks=True)
        other = ctx.cache.parent / "other-plugin/sentinel"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"foreign")
        self.w.fake.doctor = 1
        with self.assertRaises(StepFailedError):
            co.install(ctx, self.w.repo, self.w.sha)
        self.assertFalse(ctx.cache.exists())
        self.assertEqual(other.read_bytes(), b"foreign")
        rec = co.load_record(ctx.rdir / "rollback-record.json")
        rb.rollback(ctx, rec)
        self.assertFalse(ctx.cache.exists())

    def test_cache_snapshot_refuses_aliased_files_before_mutation(
        self,
    ) -> None:
        self.install()
        ctx, sha = self.upgrade()
        original = ctx.config.read_bytes()
        calls_before = len(self.w.fake.calls)
        outside = self.w.home / "outside"
        outside.write_bytes(b"foreign")
        (ctx.cache / "alias").symlink_to(outside)
        with self.assertRaises(OSError):
            co.install(ctx, self.w.repo, sha)
        self.assertEqual(outside.read_bytes(), b"foreign")
        self.assertEqual(ctx.config.read_bytes(), original)
        self.assertFalse(
            any(
                call[:3] == ["codex", "plugin", "add"]
                for call in self.w.fake.calls[calls_before:]
            )
        )

    def test_cache_restore_failure_keeps_recovery_and_revokes_trust(
        self,
    ) -> None:
        self.install()
        ctx, rec = self.before_store_preparation()
        before = {
            p: p.read_bytes() for p in ctx.cache.rglob("*") if p.is_file()
        }
        dry = dataclasses.replace(ctx, dry_run=True)
        rb.rollback(dry, rec)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        with (
            mock.patch(
                "install.rollback.shutil.copytree", side_effect=OSError
            ),
            self.assertRaises(OSError),
        ):
            rb.rollback(ctx, rec)
        self.assertNotIn("trusted_hash", ctx.config.read_text())
        self.assertTrue((ctx.rdir / "codex-cache").is_dir())
        rb.rollback(ctx, rec)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_pre_plugin_cache_drift_restores_matching_trust(self) -> None:
        self.install()
        ctx, rec = self.before_store_preparation()
        cache = ctx.cache / "0.2.0/hooks/hooks.json"
        before, config = cache.read_bytes(), ctx.config.read_bytes()
        original = steps_config._add_plugin

        def drift(context: Ctx, rec: dict[str, Any]) -> bytes:
            doc = json.loads(cache.read_bytes())
            doc["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 17
            cache.write_bytes(dump(doc))
            return original(context, rec)

        with (
            mock.patch("install.steps_config._add_plugin", drift),
            self.assertRaisesRegex(StepFailedError, "cache changed before"),
        ):
            steps_config.codex(ctx, rec)
        rb.rollback(ctx, rec)
        self.assertFalse(rec.get("codex_cache_changed", False))
        self.assertEqual(cache.read_bytes(), before)
        self.assertEqual(ctx.config.read_bytes(), config)
        self.assertEqual(
            (ctx.rdir / "codex-cache/0.2.0/hooks/hooks.json").read_bytes(),
            before,
        )
        rb.rollback(ctx, rec)
        self.assertEqual(cache.read_bytes(), before)
        self.assertEqual(ctx.config.read_bytes(), config)

    def test_changed_snapshot_refuses_restore_without_overwriting_cache(
        self,
    ) -> None:
        self.install()
        ctx, rec = self.before_store_preparation()
        cache = ctx.cache / "0.2.0/hooks/hooks.json"
        before = cache.read_bytes()
        (ctx.rdir / "codex-cache/0.2.0/hooks/hooks.json").write_bytes(
            b"incomplete"
        )
        with self.assertRaises(OSError):
            rb.rollback(ctx, rec)
        self.assertEqual(cache.read_bytes(), before)
        self.assertNotIn("trusted_hash", ctx.config.read_text())

    def test_codex_reported_hash_mismatch_refuses_install(self) -> None:
        def mismatched(ctx: Ctx) -> list[dict[str, Any]]:
            entries = self.w.fake.probe(ctx)
            entries[0]["currentHash"] = "sha256:" + "0" * 64
            return entries

        ctx = self.w.ctx(fresh=True)
        ctx.probe = mismatched
        before = ctx.config.read_bytes()
        with self.assertRaises(StepFailedError):
            co.install(ctx, self.w.repo, self.w.sha)
        self.assertEqual(ctx.config.read_bytes(), before)
