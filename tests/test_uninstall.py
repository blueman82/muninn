"""Rehearse uninstall in a temp HOME after a real fresh install."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
from typing import Any
from unittest import mock

from install import installer as co
from install import uninstall as un
from install.constants import PRE_UPGRADE_PREFIX, PRIVATE_UMASK
from install.context import StepFailedError
from install.errors import RefusedError
from tests.installer_support import World, snapshot

OWNER_GROUP = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}


class UninstallCase(unittest.TestCase):
    """A fresh install in a temp HOME, and helpers to inspect it."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.c = w.ctx()
        self.before = snapshot(w.home)
        co.install(w.ctx(fresh=True), w.repo, w.sha)

    def run_uninstall(self, **kw: Any) -> bool:
        """Uninstall with the world's fakes.

        Args:
            **kw: ``dry_run`` for the context, ``purge`` for the call.

        Returns:
            What ``uninstall`` returned.
        """
        purge = kw.pop("purge", False)
        return un.uninstall(self.w.ctx(**kw), purge)

    def edit_settings(self, edit: Any) -> None:
        """Rewrite Claude's settings.json through ``edit(obj)``."""
        obj = json.loads(self.c.settings.read_text())
        edit(obj)
        self.c.settings.write_text(json.dumps(obj, indent=2))

    def assert_untouched(self) -> None:
        """Assert the install is still whole: job, plist, release, data."""
        self.assertEqual(self.w.fake.loaded, "new")
        for path in (self.c.plist, self.c.lib, self.c.data):
            self.assertTrue(path.exists(), path)
        self.assertFalse(self.c.removed.exists())


class UninstallTest(UninstallCase):
    """A fresh install, then each way of taking it back out."""

    def test_configs_return_to_their_pre_install_bytes(self) -> None:
        self.assertTrue(self.run_uninstall())
        for rel, data in self.before.items():
            self.assertEqual((self.w.home / rel).read_bytes(), data, rel)

    def test_job_links_release_and_plugin_cache_are_gone(self) -> None:
        self.run_uninstall()
        self.assertIsNone(self.w.fake.loaded)
        self.assertFalse(self.c.plist.exists())
        self.assertFalse(os.path.lexists(self.c.muninn))
        self.assertFalse(self.c.lib.exists())
        self.assertFalse(self.c.cache.parent.exists())

    def test_data_is_moved_aside_not_deleted(self) -> None:
        self.assertTrue((self.c.data / "recall.off").exists())
        self.run_uninstall()
        self.assertTrue((self.c.removed / self.c.data.name).is_dir())
        self.assertTrue(
            (self.c.removed / self.c.data.name / "recall.off").exists()
        )
        self.assertEqual(self.c.removed.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.c.data.exists())

    def test_a_leftover_pre_upgrade_copy_goes_with_the_data(self) -> None:
        name = f"{PRE_UPGRADE_PREFIX}abc"
        (self.c.data / name).write_bytes(b"store copy")
        self.run_uninstall()
        self.assertTrue((self.c.removed / self.c.data.name / name).exists())

    def test_purge_deletes_a_leftover_pre_upgrade_copy(self) -> None:
        (self.c.data / f"{PRE_UPGRADE_PREFIX}abc").write_bytes(b"copy")
        self.run_uninstall(purge=True)
        self.assertFalse(self.c.data.exists())
        self.assertFalse(self.c.removed.exists())

    def test_purge_deletes_the_data(self) -> None:
        self.run_uninstall(purge=True)
        self.assertFalse(self.c.data.exists())
        self.assertFalse(self.c.removed.exists())

    def test_purge_with_a_dry_run_deletes_nothing(self) -> None:
        self.run_uninstall(purge=True, dry_run=True)
        self.assert_untouched()

    def test_other_hooks_and_keys_are_kept(self) -> None:
        def edit(obj: dict[str, Any]) -> None:
            obj["hooks"]["SessionStart"].append(OWNER_GROUP)
            obj["hooks"]["Stop"] = [OWNER_GROUP]

        self.edit_settings(edit)
        self.run_uninstall()
        after = json.loads(self.c.settings.read_text())
        self.assertEqual(
            after["hooks"],
            {"SessionStart": [OWNER_GROUP], "Stop": [OWNER_GROUP]},
        )
        self.assertEqual(after["theme"], "dark")

    def test_dry_run_changes_nothing(self) -> None:
        configs = snapshot(self.w.home)
        self.assertTrue(self.run_uninstall(dry_run=True))
        self.assertEqual(snapshot(self.w.home), configs)
        self.assert_untouched()
        self.assertTrue((self.c.lib / "current").exists())
        self.assertIn("DRY-RUN uninstall", "\n".join(self.w.out))
        self.assertEqual(self.w.out[-1], "dry run only: nothing was removed")

    def test_a_second_run_finds_nothing_to_do(self) -> None:
        self.run_uninstall()
        self.w.out.clear()
        self.assertFalse(self.run_uninstall())
        self.assertEqual(self.w.out, ["muninn is not installed here"])

    def test_a_foreign_muninn_link_is_left_alone(self) -> None:
        self.c.muninn.unlink()
        mine = self.w.home / "my-muninn"
        mine.write_text("#!/bin/sh\n")
        self.c.muninn.symlink_to(mine)
        self.run_uninstall()
        self.assertEqual(self.c.muninn.readlink(), mine)

    def test_a_link_into_a_dir_sharing_our_prefix_is_left_alone(self) -> None:
        self.c.muninn.unlink()
        other = self.c.lib.with_name(f"{self.c.lib.name}-other")
        other.mkdir()
        self.c.muninn.symlink_to(other / "muninn")
        self.run_uninstall()
        self.assertEqual(self.c.muninn.readlink(), other / "muninn")

    def test_a_regular_file_at_the_link_path_is_left_alone(self) -> None:
        self.c.muninn.unlink()
        self.c.muninn.write_text("#!/bin/sh\n")
        self.run_uninstall()
        self.assertEqual(self.c.muninn.read_text(), "#!/bin/sh\n")


class RefusalTest(UninstallCase):
    """What is checked before the first change, and what is not touched."""

    def test_a_hooks_value_we_cannot_edit_changes_nothing(self) -> None:
        self.edit_settings(lambda o: o["hooks"].update(SessionStart="x"))
        for dry in (False, True):
            with self.assertRaises(RefusedError):
                self.run_uninstall(dry_run=dry)
        self.assert_untouched()

    def test_a_stray_marker_in_the_codex_config_changes_nothing(self) -> None:
        text = self.c.config.read_text()
        self.c.config.write_text(text + '\n[extra]\nnote = "muninn@x"\n')
        with self.assertRaises(RefusedError):
            self.run_uninstall()
        self.assert_untouched()

    def test_a_taken_data_name_refuses_before_any_change(self) -> None:
        self.c.removed.mkdir()
        with self.assertRaises(StepFailedError):
            self.run_uninstall()
        self.assertEqual(self.w.fake.loaded, "new")
        self.assertTrue(self.c.lib.exists())
        self.assertTrue(self.c.data.exists())

    def test_main_reports_a_refusal_plainly_and_exits_1(self) -> None:
        self.edit_settings(lambda o: o["hooks"].update(SessionStart="x"))
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: ", out)
        self.assertIn("run again", out)

    def test_main_runs_the_dry_run_from_the_command_line(self) -> None:
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry run only: nothing was removed", out)
        self.assert_untouched()

    def run_main(self, *argv: str) -> tuple[int, str]:
        """Run ``main`` against the temp HOME with the fake launchd.

        Args:
            *argv: Command-line arguments.

        Returns:
            The exit status and everything it printed.
        """
        self.addCleanup(os.umask, os.umask(PRIVATE_UMASK))
        env = {"HOME": str(self.w.home)}
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(un, "run_real", self.w.fake.run),
            contextlib.redirect_stdout(out),
        ):
            rc = un.main(argv)
        return rc, out.getvalue()


class PartialStateTest(UninstallCase):
    """Machines that are missing one piece or never had the others."""

    def test_missing_provider_config_files_are_fine(self) -> None:
        self.c.settings.unlink()
        self.c.config.unlink()
        self.assertTrue(self.run_uninstall())
        self.assertFalse(self.c.lib.exists())

    def test_settings_without_our_hooks_are_not_rewritten(self) -> None:
        odd = '{"name": "caf\\u00e9", "list": [1, 2], "hooks": {}}'
        self.c.settings.write_text(odd)
        self.run_uninstall()
        self.assertEqual(self.c.settings.read_text(), odd)
        self.assertFalse(self.c.lib.exists())

    def test_settings_holding_only_our_hooks_stay_valid_json(self) -> None:
        keep = json.loads(self.c.settings.read_text())["hooks"]
        self.c.settings.write_text(json.dumps({"hooks": keep}))
        self.run_uninstall()
        self.assertEqual(json.loads(self.c.settings.read_text()), {})

    def test_a_foreign_plugin_next_to_ours_in_the_cache_survives(self) -> None:
        theirs = self.c.cache.parent / "other-plugin"
        theirs.mkdir()
        self.run_uninstall()
        self.assertFalse(self.c.cache.exists())
        self.assertTrue(theirs.is_dir())

    def test_an_absent_plugin_cache_is_fine(self) -> None:
        shutil.rmtree(self.c.cache)
        self.assertTrue(self.run_uninstall())

    def test_a_machine_with_only_a_data_dir_has_it_moved_aside(self) -> None:
        w = World(self)
        c = w.ctx()
        c.data.mkdir(parents=True)
        self.assertTrue(un.uninstall(c))
        self.assertFalse(c.data.exists())
        self.assertTrue((c.removed / c.data.name).is_dir())

    def test_a_machine_that_never_had_muninn_says_so(self) -> None:
        w = World(self)
        self.assertFalse(un.uninstall(w.ctx()))
        self.assertEqual(w.out, ["muninn is not installed here"])


if __name__ == "__main__":
    unittest.main()
