"""The pre-upgrade store copy: taken, restored on rollback, then deleted."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

from install import installer as co
from install import snapshot as sn
from install.constants import PRE_UPGRADE_PREFIX
from muninn import erase_residue, obs, platform_io, store
from tests.cli_support import fake_run
from tests.installer_support import ROOT, World, git, make_store
from tests.store_support import assert_private

TS = "20261002T000000Z"
SNAP = PRE_UPGRADE_PREFIX


def rows(path: Path) -> tuple[int, list[str]]:
    """Return a store's ``user_version`` and its markers and tables."""
    with closing(sqlite3.connect(path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master ORDER BY name"
            )
        ]
        names += [r[0] for r in conn.execute("SELECT x FROM t")]
    return version, names


class SnapshotCase(unittest.TestCase):
    """A machine on schema v1 about to upgrade to a v2 release."""

    def setUp(self) -> None:
        self.w = w = World(self)
        w.fake.store_version = 1
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/muninn"
        self.data = w.home / ".local/share/muninn"
        self.store = self.data / "muninn.sqlite"
        self.ctx = dataclasses.replace(w.ctx(upgrade=True), ts=TS)
        w.fake.migrates = self.sha2
        self.snap = self.data / f"{SNAP}{self.sha2[:12]}"

    def record(self) -> dict[str, Any]:
        """Read the install record."""
        return json.loads((self.lib / co.INSTALL_RECORD).read_text())

    def leftovers(self) -> list[str]:
        """Return pre-upgrade files and journals in the data dir."""
        return sorted(
            p.name for p in self.data.iterdir() if p.name.startswith("muninn.")
        )


class UpgradeFlowTests(SnapshotCase):
    """The copy exists while the new poller runs, and is gone after."""

    def test_good_upgrade_migrates_and_deletes_the_copy(self) -> None:
        seen: list[tuple[bool, int]] = []
        real = self.w.fake._migrate

        def spy() -> None:
            seen.append((self.snap.exists(), rows(self.snap)[0]))
            real()

        with mock.patch.object(self.w.fake, "_migrate", spy):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(seen, [(True, 1)])  # copied before the poller ran
        self.assertEqual(rows(self.store)[0], store.SCHEMA_VERSION)
        self.assertEqual(self.leftovers(), ["muninn.sqlite"])
        out = json.loads((self.lib / co.INSTALL_RECORD).read_text())
        self.assertEqual(out["snapshot"]["state"], "deleted")
        self.assertEqual(
            sorted(out["snapshot"]),
            ["bytes", "from", "name", "state", "to"],
        )

    def test_copy_is_private_and_consistent(self) -> None:
        sn.snapshot_store(
            self.ctx, {"sha": self.sha2, "schema_to": 2, "steps": []}
        )
        assert_private(self, self.snap)
        self.assertEqual(rows(self.snap), rows(self.store))

    def test_existing_private_snapshot_is_never_overwritten(self) -> None:
        self.snap.write_bytes(b"unknown prior snapshot")
        before = self.store.read_bytes()
        with self.assertRaises(co.StepFailedError):
            sn.snapshot_store(
                self.ctx, {"sha": self.sha2, "schema_to": 2, "steps": []}
            )
        self.assertEqual(self.snap.read_bytes(), b"unknown prior snapshot")
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse(self.snap.with_suffix(".tmp").exists())

    def test_no_copy_when_the_versions_match(self) -> None:
        make_store(self.data, store.SCHEMA_VERSION)
        with mock.patch.object(sn, "_copy") as copy:
            co.install(self.ctx, self.w.repo, self.sha2)
        copy.assert_not_called()
        self.assertIsNone(self.record()["snapshot"])

    def test_failed_copy_aborts_before_anything_changes(self) -> None:
        w = self.w
        with (
            mock.patch.object(sn, "_copy", side_effect=OSError("disk full")),
            self.assertRaises(co.StepFailedError) as caught,
        ):
            co.install(self.ctx, w.repo, self.sha2)
        self.assertIn("nothing was changed", str(caught.exception))
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertEqual(rows(self.store)[0], 1)
        self.assertEqual(self.leftovers(), ["muninn.sqlite"])
        kick = ["launchctl", "kickstart", "-k", self.ctx.target]
        self.assertNotIn(kick, w.fake.calls)
        self.assertFalse((self.lib / self.sha2).exists())

    def test_unreadable_store_aborts_too(self) -> None:
        self.store.write_bytes(b"not a database" * 100)
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))

    def test_delete_failure_warns_and_doctor_flags_the_copy(self) -> None:
        real = Path.unlink

        def stuck(path: Path, missing_ok: bool = False) -> None:
            if path.name.startswith(SNAP) and not path.name.endswith(".tmp"):
                raise PermissionError("busy")
            real(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", stuck):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertIn("remove it by hand", "\n".join(self.w.out))
        self.assertEqual(self.record()["snapshot"]["state"], "kept")
        with mock.patch.object(obs, "run", fake_run()):
            checks = {
                c["check"]: c
                for c in obs.doctor(self.data, {"HOME": "/x"})["checks"]
            }
        self.assertIs(checks["upgrade_snapshot"]["ok"], False)
        self.assertEqual(checks["upgrade_snapshot"]["level"], "warn")
        self.assertIs(checks["unexpected_files"]["ok"], True)


class RollbackTests(SnapshotCase):
    """A failed upgrade after the migration restores the old store."""

    def check_back_on_v1(self) -> None:
        """Assert the v1 store and the first release are live again."""
        w = self.w
        self.assertEqual(rows(self.store), (1, ["t", "row"]))
        assert_private(self, self.store)
        self.assertEqual(self.leftovers(), ["muninn.sqlite"])
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertEqual(w.fake.loaded, "new")
        self.assertTrue(
            any(c[:2] == ["launchctl", "bootstrap"] for c in w.fake.calls)
        )

    def test_failed_verify_restores_the_v1_store(self) -> None:
        self.w.fake.doctor = 1
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.check_back_on_v1()
        self.assertEqual(self.record()["snapshot"]["state"], "restored")
        self.assertEqual(self.record()["outcome"], "rolled_back")

    def test_residual_journal_retains_both_stores_and_release_selection(
        self,
    ) -> None:
        fake = self.w.fake
        fake.doctor = 1
        real = fake._migrate
        migrated: list[bytes] = []
        prior_calls = len(fake.calls)

        def crash_mid_write() -> None:
            real()
            migrated.append(self.store.read_bytes())
            journal = self.data / "muninn.sqlite-journal"
            fd = platform_io.open_private(
                journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"stale")

        with (
            mock.patch.object(fake, "_migrate", crash_mid_write),
            self.assertRaisesRegex(OSError, "residual SQLite"),
        ):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(self.store.read_bytes(), migrated[0])
        self.assertEqual(rows(self.snap), (1, ["t", "row"]))
        self.assertEqual(
            (self.data / "muninn.sqlite-journal").read_bytes(), b"stale"
        )
        self.assertEqual((self.lib / "current").readlink(), Path(self.first))
        self.assertTrue((self.lib / self.first).is_dir())
        self.assertTrue((self.lib / self.sha2).is_dir())
        self.assertFalse(
            any(
                call[:2] == ["launchctl", "bootstrap"]
                for call in fake.calls[prior_calls:]
            )
        )

    def test_failed_restart_restores_the_v1_store(self) -> None:
        self.w.fake.heartbeat = False
        self.w.fake.clock[0] += 1000
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.check_back_on_v1()

    def test_unmigrated_store_is_kept_and_the_copy_deleted(self) -> None:
        self.w.fake.migrates = None
        self.w.fake.doctor = 1
        make_store(self.data, 1, marker="newer")
        with self.assertRaises(co.StepFailedError):
            co.install(self.ctx, self.w.repo, self.sha2)
        self.assertEqual(rows(self.store), (1, ["t", "newer"]))
        self.assertEqual(self.leftovers(), ["muninn.sqlite"])
        self.assertEqual(self.record()["snapshot"]["state"], "deleted")


class PlanAndNamesTests(SnapshotCase):
    """Dry run, the schema constant, and the shared file name."""

    def test_dry_run_says_so_and_writes_nothing(self) -> None:
        before = sorted(p.name for p in self.data.iterdir())
        ctx = dataclasses.replace(self.ctx, dry_run=True)
        self.w.out.clear()
        co.install(ctx, self.w.repo, self.sha2)
        said = "\n".join(self.w.out)
        self.assertIn("would copy the store (schema v1", said)
        self.assertIn(f"{SNAP}{self.sha2[:12]}", said)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), before)
        self.assertEqual(rows(self.store)[0], 1)

    def test_release_schema_matches_the_runtime_constant(self) -> None:
        w = self.w
        self.assertEqual(
            sn.release_schema(self.ctx, w.repo, self.sha2),
            store.SCHEMA_VERSION,
        )
        text = (ROOT / sn.SCHEMA_FILE).read_text()
        found = sn._VERSION.findall(text.encode())
        self.assertEqual([int(v) for v in found], [store.SCHEMA_VERSION])

    def test_the_prefix_is_the_same_in_the_installer_and_the_runtime(
        self,
    ) -> None:
        self.assertEqual(PRE_UPGRADE_PREFIX, store.PRE_UPGRADE_PREFIX)

    def test_erase_reports_a_leftover_copy(self) -> None:
        self.snap.write_bytes(b"x")
        self.assertEqual(
            erase_residue.aside_files(self.data), [str(self.snap)]
        )


if __name__ == "__main__":
    unittest.main()
