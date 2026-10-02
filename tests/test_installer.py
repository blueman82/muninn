"""Rehearse install, upgrade and rollback in a temp HOME."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from install import installer as co
from install import rollback as rb
from tests.installer_support import (
    CRED,
    World,
    codex_view,
    git,
    snapshot,
)


class FreshInstallTest(unittest.TestCase):
    """--fresh: a new machine with plain Claude and Codex config."""

    def setUp(self) -> None:
        self.w = World(self)
        self.lib = self.w.home / ".local/lib/muninn"

    def install(self, **kw: Any) -> Any:
        """Run a fresh install of the world's repo.

        Args:
            **kw: Extra ``Ctx`` fields such as ``dry_run``.

        Returns:
            The install record.
        """
        return co.install(
            self.w.ctx(fresh=True, **kw), self.w.repo, self.w.sha
        )

    def test_install_record_and_rollback_leave_nothing_behind(self) -> None:
        w, h = self.w, self.w.home
        before = snapshot(h)
        rec = self.install()
        self.assertEqual(w.fake.loaded, "new")
        python = (self.lib / "python").readlink()
        self.assertEqual(python, Path(sys.executable))
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(
            (out["outcome"], out["fresh"], out["sha"], out["python"]["path"]),
            ("ok", True, w.sha, sys.executable),
        )
        self.assertIn("hooks.SessionStart", out["config_keys"])
        # The record is shared with colleagues, so it must hold no secret.
        self.assertNotIn(CRED, json.dumps(out))
        self.assertEqual(
            (self.lib / co.INSTALL_RECORD).stat().st_mode & 0o777, 0o600
        )
        s = json.loads((h / ".claude/settings.json").read_bytes())
        (group,) = s["hooks"]["SessionStart"]
        self.assertEqual(
            group["hooks"][0]["command"],
            f"{h}/.local/bin/muninn hook session-start --provider claude",
        )
        text = (h / ".codex/config.toml").read_text()
        self.assertEqual(
            codex_view(text),
            (f"{self.lib}/current/integrations/codex", True, 2),
        )
        record = Path(rec["rdir"]) / "rollback-record.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"record in {rec['rdir']}", "\n".join(w.out))
        rb.rollback(w.ctx(), co.load_record(record))
        for rel, data in before.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        self.assertFalse((h / ".local/share/muninn").exists())
        self.assertFalse((h / co.PLIST).exists())
        self.assertFalse(os.path.lexists(self.lib / "python"))
        self.assertFalse(os.path.lexists(h / ".local/bin/muninn"))

    def test_fresh_install_starts_with_recall_off_and_says_how_to_enable(
        self,
    ) -> None:
        flag = self.w.home / ".local/share/muninn/recall.off"
        self.install()
        self.assertEqual(flag.stat().st_mode & 0o777, 0o600)
        self.assertEqual(flag.read_bytes(), b"")
        said = "\n".join(self.w.out)
        self.assertIn(f"unlink {flag}", said)
        self.assertIn("docs/adr/0007", said)

    def test_dry_run_creates_no_recall_switch(self) -> None:
        self.install(dry_run=True)
        self.assertFalse((self.w.home / ".local/share").exists())

    def test_trust_auto_only_for_verified_codex_versions(self) -> None:
        self.w.fake.version = b"codex-cli 9.9.9\n"
        self.assertEqual(self.install()["trust"], "owner")
        self.assertTrue(any("OWNER STEP" in x for x in self.w.out))

    def test_no_claude_or_codex_is_left_unconfigured(self) -> None:
        w, h = self.w, self.w.home
        (h / ".claude/settings.json").unlink()
        (h / ".codex/config.toml").unlink()
        rec = self.install()
        self.assertEqual((rec["has_claude"], rec["has_codex"]), (False, False))
        self.assertFalse((h / ".claude/settings.json").exists())
        self.assertTrue(any("left unconfigured" in x for x in w.out))
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(out["config_keys"], [])

    def test_refuses_when_already_installed(self) -> None:
        h = self.w.home
        (h / ".local/share/muninn").mkdir(parents=True)
        with self.assertRaises(co.StepFailedError):
            self.install()
        self.assertFalse(self.lib.exists())

    def test_refuses_a_commit_that_breaks_the_standards(self) -> None:
        w = self.w
        (w.repo / "muninn").mkdir()
        (w.repo / "muninn/sloppy.py").write_text("x = 1\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "sloppy")
        sha = git(w.repo, "rev-parse", "HEAD").decode().strip()
        with self.assertRaises(co.StepFailedError) as caught:
            co.install(w.ctx(fresh=True), w.repo, sha)
        self.assertIn("standards violation", str(caught.exception))
        self.assertFalse(self.lib.exists())

    def test_dry_run_changes_nothing_and_touches_no_service(self) -> None:
        h = self.w.home
        before = snapshot(h)
        self.install(dry_run=True)
        for rel, data in before.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        self.assertFalse((h / ".local/share/muninn").exists())
        # `launchctl print` only reads state; every other verb mutates it.
        self.assertFalse(
            any(
                c[0] == "launchctl" and c[1] != "print"
                for c in self.w.fake.calls
            )
        )

    def test_failed_start_rolls_back_and_records_it(self) -> None:
        w, h = self.w, self.w.home
        w.fake.heartbeat = False
        with self.assertRaises(co.StepFailedError):
            self.install()
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(out["outcome"], "rolled_back")
        self.assertEqual(out["failed"]["step"], "start_new")
        self.assertFalse((h / ".local/share/muninn").exists())
        self.assertEqual(w.fake.loaded, None)

    def test_failed_doctor_rolls_back(self) -> None:
        self.w.fake.doctor = 1
        with self.assertRaises(co.StepFailedError):
            self.install()
        self.assertFalse((self.w.home / ".local/share/muninn").exists())

    def test_wrong_codex_answer_rolls_back(self) -> None:
        self.w.fake.probe_mode = "wrong"
        with self.assertRaises(co.StepFailedError):
            self.install()

    def test_silent_probe_leaves_the_owner_step(self) -> None:
        self.w.fake.probe_mode = "error"
        self.assertEqual(self.install()["trust"], "owner")

    def test_home_substituted_in_pinned_copy_never_in_repo(self) -> None:
        self.install()
        pinned = self.lib / "current/integrations/claude/settings-hooks.json"
        self.assertNotIn(b"@HOME@", pinned.read_bytes())
        repo = self.w.repo / "integrations/claude/settings-hooks.json"
        self.assertIn(b"@HOME@", repo.read_bytes())

    def test_inherited_muninn_env_never_reaches_muninn(self) -> None:
        with mock.patch.dict(os.environ, {"MUNINN_ROOTS": "{}"}):
            # Fake._muninn asserts MUNINN_ROOTS is absent from the child env.
            self.install()


class UpgradeTest(unittest.TestCase):
    """--upgrade: re-pin a newer commit and keep exactly one release."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/muninn"
        self.ctx = dataclasses.replace(
            w.ctx(upgrade=True), ts="20261002T000000Z"
        )

    def releases(self) -> list[str]:
        """List the pinned release directories, ignoring symlinks.

        Returns:
            Sorted directory names under the lib dir.
        """
        return sorted(
            p.name
            for p in self.lib.iterdir()
            if p.is_dir() and not p.is_symlink()
        )

    def test_one_release_remains_and_config_is_untouched(self) -> None:
        w, h = self.w, self.w.home
        self.assertEqual(self.releases(), [self.first])
        configs = snapshot(h)
        shares = sorted(p.name for p in (h / ".local/share").iterdir())
        co.install(self.ctx, w.repo, self.sha2)
        self.assertEqual(self.releases(), [self.sha2])
        self.assertEqual((self.lib / "current").readlink(), Path(self.sha2))
        kick = ["launchctl", "kickstart", "-k", self.ctx.target]
        self.assertIn(kick, w.fake.calls)
        for rel, data in configs.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(
            (out["outcome"], out["upgrade"], out["sha"]),
            ("ok", True, self.sha2),
        )
        self.assertEqual(
            sorted(p.name for p in (h / ".local/share").iterdir()),
            shares,  # no leftover record dir
        )

    def test_upgrade_says_where_the_install_record_is(self) -> None:
        """The record path is absolute, like the log path the wrapper shows."""
        w = self.w
        co.install(self.ctx, w.repo, self.sha2)
        record = self.lib / co.INSTALL_RECORD
        self.assertIn(f"record in {record}", "\n".join(w.out))

    def test_upgrade_leaves_the_recall_switch_as_the_owner_set_it(
        self,
    ) -> None:
        w = self.w
        flag = w.home / ".local/share/muninn/recall.off"
        flag.unlink()  # the owner turned recall on
        co.install(self.ctx, w.repo, self.sha2)
        self.assertFalse(flag.exists())

    def test_failed_upgrade_returns_to_the_old_release_and_keeps_running(
        self,
    ) -> None:
        w = self.w
        w.fake.heartbeat = False
        # The first install's heartbeat must look stale or the restart
        # would be judged healthy without the new job ever writing one.
        w.fake.clock[0] += 1000
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, w.repo, self.sha2)
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertTrue((self.lib / self.first).is_dir())
        self.assertEqual(w.fake.loaded, "new")  # the job was not left stopped
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(out["outcome"], "rolled_back")

    def test_refuses_when_nothing_is_installed(self) -> None:
        (self.lib / "current").unlink()
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)


if __name__ == "__main__":
    unittest.main()
