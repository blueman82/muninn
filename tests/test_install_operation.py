"""The install lock stops an upgrade when its file is not safe or changes."""

from __future__ import annotations

import dataclasses
import os
import unittest
from pathlib import Path
from unittest import mock

from install import installer as co
from install.context import Ctx
from install.record import Record
from tests.installer_support import World, git


class InstallLockTest(unittest.TestCase):
    """An upgrade refuses a lock file that is unsafe or replaced."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/muninn"
        self.lock = self.lib / ".install-record.lock"
        self.ctx = dataclasses.replace(
            w.ctx(upgrade=True), ts="20261002T000000Z"
        )

    def upgrade_refuses(self, code: str) -> None:
        """Require the upgrade to stop with one code before any change.

        Args:
            code: The expected refusal code.
        """
        calls = len(self.w.fake.calls)
        with self.assertRaisesRegex(co.StepFailedError, f"^{code}$"):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertEqual(self.w.fake.calls[calls:], [])

    def test_hard_linked_lock_is_unsafe(self) -> None:
        os.link(self.lock, self.lib / "second-link")
        self.upgrade_refuses("install_lock_unsafe")

    def test_loose_mode_lock_is_unsafe(self) -> None:
        self.lock.chmod(0o644)
        self.upgrade_refuses("install_lock_unsafe")

    def test_lock_swapped_mid_run_stops_before_restart(self) -> None:
        swapped: list[int] = []

        def swap_lock(ctx: Ctx, rec: Record) -> None:
            """Replace the lock file with a new private file.

            Args:
                ctx: The run context.
                rec: The run record.
            """
            other = self.lib / "other.lock"
            other.write_bytes(b"")
            other.chmod(0o600)
            other.replace(self.lock)
            swapped.append(len(self.w.fake.calls))

        order = list(co.UPGRADE_STEPS)
        order.insert(order.index(co.restart), swap_lock)
        with (
            mock.patch.object(co, "steps", return_value=tuple(order)),
            self.assertRaisesRegex(
                co.StepFailedError, "^install_lock_changed$"
            ),
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(len(swapped), 1)
        later = self.w.fake.calls[swapped[0] :]
        self.assertFalse([c for c in later if c[0] == "launchctl"], later)
        self.assertIsNone(self.w.fake.loaded)


if __name__ == "__main__":
    unittest.main()
