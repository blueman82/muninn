"""Rehearse uninstall in a temp HOME after a real fresh install."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from install import installer as co
from install import uninstall as un
from install.constants import REMOVED_PREFIX
from tests.installer_support import World, snapshot

OWNER_GROUP = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}


class UninstallTest(unittest.TestCase):
    """A fresh install, then each way of taking it back out."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.home = w.home
        self.before = snapshot(w.home)
        co.install(w.ctx(fresh=True), w.repo, w.sha)
        self.lib = w.home / ".local/lib/muninn"
        self.share = w.home / ".local/share"

    def run_uninstall(self, **kw: Any) -> bool:
        """Uninstall with the world's fakes.

        Args:
            **kw: ``dry_run`` for the context, ``purge`` for the call.

        Returns:
            What ``uninstall`` returned.
        """
        purge = kw.pop("purge", False)
        return un.uninstall(self.w.ctx(**kw), purge)

    def test_configs_return_to_their_pre_install_bytes(self) -> None:
        self.assertTrue(self.run_uninstall())
        for rel, data in self.before.items():
            self.assertEqual((self.home / rel).read_bytes(), data, rel)

    def test_job_links_release_and_plugin_cache_are_gone(self) -> None:
        self.run_uninstall()
        self.assertIsNone(self.w.fake.loaded)
        self.assertFalse((self.home / co.PLIST).exists())
        self.assertFalse(os.path.lexists(self.home / ".local/bin/muninn"))
        self.assertFalse(self.lib.exists())
        cache = self.home / ".codex/plugins/cache"
        self.assertFalse((cache / "muninn-local").exists())

    def test_data_is_moved_aside_not_deleted(self) -> None:
        marker = self.share / "muninn/recall.off"
        self.assertTrue(marker.exists())
        self.run_uninstall()
        kept = self.share / f"{REMOVED_PREFIX}{self.w.ctx().ts}"
        self.assertTrue((kept / "data/recall.off").exists())
        self.assertEqual(kept.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.share / "muninn").exists())

    def test_purge_deletes_the_data(self) -> None:
        self.run_uninstall(purge=True)
        self.assertFalse((self.share / "muninn").exists())
        self.assertEqual(
            [p.name for p in self.share.glob(f"{REMOVED_PREFIX}*")], []
        )

    def test_other_hooks_and_keys_are_kept(self) -> None:
        path = self.home / ".claude/settings.json"
        obj = json.loads(path.read_text())
        obj["hooks"]["SessionStart"].append(OWNER_GROUP)
        obj["hooks"]["Stop"] = [OWNER_GROUP]
        path.write_text(json.dumps(obj, indent=2))
        self.run_uninstall()
        after = json.loads(path.read_text())
        self.assertEqual(
            after["hooks"],
            {"SessionStart": [OWNER_GROUP], "Stop": [OWNER_GROUP]},
        )
        self.assertEqual(after["theme"], "dark")

    def test_dry_run_changes_nothing(self) -> None:
        configs = snapshot(self.home)
        self.assertTrue(self.run_uninstall(dry_run=True))
        self.assertEqual(snapshot(self.home), configs)
        self.assertEqual(self.w.fake.loaded, "new")
        self.assertTrue((self.home / co.PLIST).exists())
        self.assertTrue((self.lib / "current").exists())
        self.assertTrue((self.share / "muninn").exists())
        self.assertIn("DRY-RUN uninstall", "\n".join(self.w.out))
        self.assertEqual(self.w.out[-1], "dry run only: nothing was removed")

    def test_a_second_run_finds_nothing_to_do(self) -> None:
        self.run_uninstall()
        self.w.out.clear()
        self.assertFalse(self.run_uninstall())
        self.assertEqual(self.w.out, ["muninn is not installed here"])

    def test_a_foreign_muninn_link_is_left_alone(self) -> None:
        link = self.home / ".local/bin/muninn"
        link.unlink()
        mine = self.home / "my-muninn"
        mine.write_text("#!/bin/sh\n")
        link.symlink_to(mine)
        self.run_uninstall()
        self.assertEqual(link.readlink(), mine)


if __name__ == "__main__":
    unittest.main()
