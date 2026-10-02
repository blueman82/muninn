"""Upgrading a pre-rename install moves it onto the muninn names."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from install import installer as co
from tests.installer_legacy_support import (
    ROWS,
    old_state,
    seed_old,
    store_counts,
)
from tests.installer_support import World, codex_view, snapshot


class MigrateTest(unittest.TestCase):
    """--upgrade on a machine still on the pre-rename names."""

    def setUp(self) -> None:
        self.w = World(self)
        self.h = self.w.home
        seed_old(self.w)
        self.before = snapshot(self.h)

    def upgrade(self, **kw: object) -> object:
        """Run the upgrade of the world's repo."""
        ctx = self.w.ctx(upgrade=True, **kw)
        return co.install(ctx, self.w.repo, self.w.sha)

    def test_migrates_data_job_hooks_codex_and_removes_the_old(self) -> None:
        h = self.h
        self.assertTrue(self.w.ctx(upgrade=True).legacy)
        self.upgrade()
        self.assertEqual(set(old_state(h).values()), {False})
        self.assertFalse((h / ".local/share/provenance-context").exists())
        data = h / ".local/share/muninn"
        self.assertEqual(store_counts(data / "muninn.sqlite"), ROWS)
        self.assertFalse((data / "pctx.sqlite").exists())
        self.assertTrue((data / "recall.off").exists())  # owner's choice kept
        self.assertEqual(self.w.fake.loaded, "new")
        self.assertTrue((h / ".local/bin/muninn").is_symlink())
        settings = json.loads((h / ".claude/settings.json").read_bytes())
        cmds = [
            x["command"]
            for e in settings["hooks"].values()
            for g in e
            for x in g["hooks"]
        ]
        self.assertIn("/usr/bin/true", cmds)  # a foreign hook survives
        self.assertEqual(sum("muninn hook" in c for c in cmds), 2)
        self.assertFalse(any("pctx" in c for c in cmds))
        text = (h / ".codex/config.toml").read_text()
        self.assertNotIn("provenance-context", text)
        self.assertEqual(codex_view(text)[1:], (True, 2))

    def test_a_failed_verify_leaves_the_old_install_runnable(self) -> None:
        h = self.h
        self.w.fake.doctor = 1
        with self.assertRaises(co.StepFailedError):
            self.upgrade()
        self.assertEqual(set(old_state(h).values()), {True})
        self.assertEqual(self.w.fake.loaded, "old")
        self.assertEqual(
            store_counts(h / ".local/share/provenance-context/pctx.sqlite"),
            ROWS,
        )
        self.assertEqual(snapshot(h), self.before)
        self.assertFalse((h / ".local/share/muninn").exists())
        self.assertFalse(os.path.lexists(h / ".local/bin/muninn"))
        self.assertFalse(
            (h / "Library/LaunchAgents/com.muninn.plist").exists()
        )

    def test_busy_old_store_is_refused_and_the_old_job_restarts(self) -> None:
        lock = self.h / ".local/share/provenance-context/writer.lock"
        fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(co.StepFailedError) as caught:
            self.upgrade()
        self.assertIn("busy", str(caught.exception))
        self.assertEqual(self.w.fake.loaded, "old")
        self.assertEqual(set(old_state(self.h).values()), {True})
        self.assertFalse((self.h / ".local/share/muninn").exists())

    def test_a_hot_journal_is_refused_before_anything_changes(
        self,
    ) -> None:
        db = self.h / ".local/share/provenance-context/pctx.sqlite"
        journal = db.with_name("pctx.sqlite-journal")
        journal.write_text("hot")
        with self.assertRaises(co.StepFailedError) as caught:
            self.upgrade()
        self.assertIn("hot journal", str(caught.exception))
        self.assertEqual(self.w.fake.loaded, "old")

    def test_running_again_after_success_is_a_plain_upgrade(self) -> None:
        self.upgrade()
        calls = len(self.w.fake.calls)
        ctx = dataclasses.replace(
            self.w.ctx(upgrade=True), ts="20261002T000000Z"
        )
        self.assertFalse(ctx.legacy)
        co.install(ctx, self.w.repo, self.w.sha)
        self.assertGreater(len(self.w.fake.calls), calls)
        self.assertEqual(set(old_state(self.h).values()), {False})
        self.assertEqual(
            store_counts(self.h / ".local/share/muninn/muninn.sqlite"), ROWS
        )

    def test_preview_says_what_will_move_and_writes_nothing(self) -> None:
        self.upgrade(dry_run=True)
        said = "\n".join(self.w.out)
        self.assertIn("would stop the old poller", said)
        self.assertIn("pctx.sqlite becomes muninn.sqlite", said)
        self.assertIn("then delete the old install", said)
        self.assertIn("(no way back)", said)
        self.assertIn("remove the old pctx hook entries", said)
        self.assertIn("remove the old provenance-context marketplace", said)
        self.assertIn("nothing was written", said)
        self.assertEqual(set(old_state(self.h).values()), {True})
        self.assertEqual(snapshot(self.h), self.before)
        self.assertFalse((self.h / ".local/share/muninn").exists())
        mutating = [
            c
            for c in self.w.fake.calls
            if c[:2] != ["launchctl", "print"] and c[0] == "launchctl"
        ]
        self.assertEqual(mutating, [])

    def test_fresh_install_refuses_over_an_old_install(self) -> None:
        with self.assertRaises(co.StepFailedError) as caught:
            co.install(self.w.ctx(fresh=True), self.w.repo, self.w.sha)
        self.assertIn("pre-rename", str(caught.exception))

    def test_both_layouts_present_is_refused_and_names_the_dir(self) -> None:
        new = self.h / ".local/share/muninn"
        new.mkdir(parents=True)
        (new / "x").write_text("x")
        with self.assertRaises(co.StepFailedError) as caught:
            self.upgrade()
        msg = str(caught.exception)
        self.assertIn(str(new), msg)
        self.assertIn("untouched", msg)
        self.assertEqual(self.w.fake.loaded, "old")

    def test_a_lone_install_log_does_not_block_the_move(self) -> None:
        lib = self.h / ".local/lib/muninn"
        lib.mkdir(parents=True)
        (lib / "install.log").write_text("earlier failed run\n")
        self.assertTrue(self.w.ctx(upgrade=True).legacy)
        self.upgrade()
        self.assertEqual(set(old_state(self.h).values()), {False})
        self.assertTrue((lib / "current").is_symlink())

    def test_old_hook_calling_the_release_path_directly_is_ours(self) -> None:
        cmd = f"{self.h}/.local/lib/provenance-context/current/bin/pctx hook x"
        group = {"hooks": [{"type": "command", "command": cmd}]}
        self.assertTrue(co.ours(group))
        other = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
        self.assertFalse(co.ours(other))

    def test_an_empty_stray_data_dir_does_not_block(self) -> None:
        (self.h / ".local/share/muninn").mkdir(parents=True)
        self.upgrade()
        self.assertEqual(set(old_state(self.h).values()), {False})

    def test_rows_written_to_the_old_store_after_the_copy_are_kept(
        self,
    ) -> None:
        db = self.h / ".local/share/provenance-context/pctx.sqlite"
        real = co.legacy_remove

        def late_write(ctx: co.Ctx) -> None:
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO events (t) VALUES ('late')")
            conn.commit()
            conn.close()
            real(ctx)

        with (
            mock.patch.object(co, "legacy_remove", late_write),
            self.assertRaises(co.StepFailedError) as caught,
        ):
            self.upgrade()
        self.assertIn("changed after it was copied", str(caught.exception))
        self.assertTrue(db.exists())
        self.assertTrue(
            (self.h / ".local/lib/provenance-context/current").exists()
        )
        self.assertEqual(store_counts(db)["events"], ROWS["events"] + 1)

    def test_a_failed_removal_is_resumed_by_the_next_run(self) -> None:
        real = shutil.rmtree

        def flaky(path: Path, *a: object, **k: object) -> None:
            if Path(path).name == "provenance-context-local":
                raise PermissionError("denied")
            real(path)

        with (
            mock.patch.object(shutil, "rmtree", flaky),
            self.assertRaises(co.StepFailedError),
        ):
            self.upgrade()
        old_data = self.h / ".local/share/provenance-context/pctx.sqlite"
        self.assertTrue(old_data.exists())  # the data dir goes last
        self.upgrade()
        self.assertEqual(set(old_state(self.h).values()), {False})
        self.assertFalse((self.h / ".local/share/provenance-context").exists())
        self.assertEqual(
            store_counts(self.h / ".local/share/muninn/muninn.sqlite"), ROWS
        )


if __name__ == "__main__":
    unittest.main()
