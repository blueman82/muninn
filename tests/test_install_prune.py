"""Releasing superseded versions: prune, its undo, the sweep and doctor."""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from unittest import mock

from install import installer as co
from install import rollback as rb
from install import steps_release as sr
from muninn import obs
from tests.installer_support import World, git

TS = "20261002T000000Z"


class PruneTest(unittest.TestCase):
    """An upgrade over a lib dir holding stale releases."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/muninn"
        self.ctx = dataclasses.replace(w.ctx(upgrade=True), ts=TS)
        self.stale = [self.lib / ("0" * 40), self.lib / ("1" * 40)]
        for path in self.stale:
            path.mkdir()
            (path / "f").write_text("x")

    def pruning(self) -> list[str]:
        """Return the names of the parked release dirs."""
        return sorted(p.name for p in self.lib.glob(f"{sr.PRUNING}*"))

    def rename_failing(self, fail: set[str]) -> mock._patch[object]:
        """Patch ``Path.rename`` to refuse the moves named in ``fail``."""
        real = Path.rename

        def flaky(src: Path, dst: Path) -> Path:
            if src.name in fail:
                raise PermissionError("denied")
            return real(src, dst)

        return mock.patch.object(Path, "rename", flaky)

    def test_the_first_rename_failing_leaves_every_release_whole(self) -> None:
        first_name = sorted(p.name for p in [*self.stale])[0]
        with (
            self.rename_failing({first_name}),
            self.assertRaises(PermissionError),
        ):
            sr.prune(self.ctx, {"sha": self.sha2})
        self.assertEqual(self.pruning(), [])
        self.assertTrue(all((p / "f").exists() for p in self.stale))

    def test_a_failed_undo_is_named_and_the_original_error_is_raised(
        self,
    ) -> None:
        # Moves go in sorted order: "000..." and the first release's own
        # name sort among them, so fail the undo of whichever moved first.
        order = sorted(
            p.name
            for p in self.lib.iterdir()
            if p.is_dir() and not p.is_symlink() and p.name != self.sha2
        )
        parked = f"{sr.PRUNING}{TS}-{order[0]}"
        real = Path.rename
        calls = {"n": 0}

        def flaky(src: Path, dst: Path) -> Path:
            if src.name == order[-1]:
                raise PermissionError("the original")
            if src.name == parked:  # the undo of an earlier move
                calls["n"] += 1
                raise OSError("undo denied")
            return real(src, dst)

        with (
            mock.patch.object(Path, "rename", flaky),
            self.assertRaises(PermissionError) as raised,
        ):
            sr.prune(self.ctx, {"sha": self.sha2})
        self.assertIn("the original", str(raised.exception))
        self.assertEqual(calls["n"], 1)
        said = "\n".join(self.w.out)
        self.assertIn(parked, said)
        self.assertIn("could not move back", said)
        self.assertEqual(self.pruning(), [parked])

    def test_prune_leaves_parked_dirs_alone(self) -> None:
        old = self.lib / f"{sr.PRUNING}20250101T000000Z-{'2' * 40}"
        old.mkdir()
        sr.prune(self.ctx, {"sha": self.sha2})
        self.assertTrue(old.is_dir())
        names = self.pruning()
        self.assertEqual(len(names), 4)  # two stale, the first, the old one
        self.assertFalse(any(f"-{sr.PRUNING}" in n for n in names))

    def test_a_dry_run_says_what_it_would_do_and_moves_nothing(self) -> None:
        dry = dataclasses.replace(self.ctx, dry_run=True)
        sr.prune(dry, {"sha": self.sha2})
        said = "\n".join(self.w.out)
        for name in (self.first, *(p.name for p in self.stale)):
            self.assertIn(
                f"would move the previous release {name} aside, then"
                " delete it once the install succeeds (no way back)",
                said,
            )
        self.assertEqual(self.pruning(), [])

    def test_the_next_install_sweeps_what_a_failed_delete_left(self) -> None:
        real = sr.shutil.rmtree

        def stuck(path: Path) -> None:
            if Path(path).name.startswith(sr.PRUNING):
                raise OSError("busy")
            real(path)

        with mock.patch.object(sr.shutil, "rmtree", stuck):
            rec = co.install(self.ctx, self.w.repo, self.sha2)
        left = self.pruning()
        self.assertEqual(len(left), 3)
        self.assertEqual(sorted(rec["sweep_failed"]), left)
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(sorted(out["sweep_failed"]), left)
        later = dataclasses.replace(self.ctx, ts="20261003T000000Z")
        rec = co.install(later, self.w.repo, self.sha2)
        self.assertEqual(self.pruning(), [])
        self.assertEqual(rec["sweep_failed"], [])

    def test_a_killed_run_is_rolled_back_to_a_release_that_exists(
        self,
    ) -> None:
        sr.prune(self.ctx, {"sha": self.sha2})
        self.assertFalse((self.lib / self.first).exists())
        record = {"links": {str(self.lib / "current"): self.first}}
        rb.rollback(self.ctx, record)
        self.assertTrue((self.lib / self.first / "bin").is_dir())
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertTrue(all((p / "f").exists() for p in self.stale))
        self.assertEqual(self.pruning(), [])


class DoctorLeftoverTests(unittest.TestCase):
    """Doctor warns while a parked release is still on disk."""

    def test_the_prefix_matches_the_installers(self) -> None:
        self.assertEqual(obs.PRUNING, sr.PRUNING)

    def test_warns_only_while_one_is_left(self) -> None:
        w = World(self)
        env = {"HOME": str(w.home)}
        lib = w.home / ".local/lib/muninn"
        lib.mkdir(parents=True)
        self.assertTrue(obs._release_leftovers(env)["ok"])
        (lib / f"{sr.PRUNING}{TS}-abc").mkdir()
        got = obs._release_leftovers(env)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["level"], "warn")
        self.assertEqual(got["detail"], f"{sr.PRUNING}{TS}-abc")


if __name__ == "__main__":
    unittest.main()
