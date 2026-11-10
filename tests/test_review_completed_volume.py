"""Recover completed macOS owners without a second computer boot."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing
from pathlib import Path
from unittest import mock

from muninn import cli_recovery, store
from muninn import review_pause as pause
from muninn import review_pause_state as state
from tests.review_pause_support import initialize, interrupt_erasure

BOOT = "darwin:12345678-1234-4234-8234-123456789abc"
VOLUME = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"
OTHER_VOLUME = "1c78c237-1f57-4ada-acac-c2e7630192c8"


@unittest.skipIf(os.name == "nt", "macOS migration uses POSIX locks")
class CompletedVolumeTests(unittest.TestCase):
    """Keep completion, disk confirmation and erasure protection together."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        for target, value in (
            ("muninn.review_pause_state.sys.platform", "darwin"),
            ("muninn.cli_recovery._platform_name", "darwin"),
            ("muninn.review_volume_identity.observe", VOLUME),
            ("muninn.review_boot_identity.observe", BOOT),
        ):
            patcher = (
                mock.patch(target, return_value=value)
                if not target.endswith("sys.platform")
                else mock.patch(target, value)
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        initialize(self.home)

    def change_device(self, *, legacy: bool = False) -> None:
        """Model a prior device number while keeping the actual inode."""
        path = self.home / state.WITNESS
        value = json.loads(path.read_bytes())
        value["lock_identity"][0] += 4
        if legacy:
            value.pop("volume_uuid", None)
        store.write_json_atomic(path, value)

    def baseline(self) -> None:
        """Capture the same repair authority as the first refused command."""
        _, predecessor = state.inspect(self.home, state.WITNESS)
        fd = state.acquire(self.home)
        try:
            state.publish(
                self.home,
                fd,
                state.BASELINE,
                state.Baseline(
                    version=1,
                    lock_identity=state.lock_identity(self.home, fd),
                    boot=BOOT,
                    predecessor=predecessor,
                    volume_uuid=VOLUME,
                ),
            )
        finally:
            os.close(fd)

    def recover(
        self, *, confirmed: bool, bind_volume: bool = True
    ) -> tuple[int, object]:
        """Run the public recovery handler against the private fixture."""
        return cli_recovery.recover(
            Namespace(
                confirm_legacy_volume=confirmed,
                bind_witness_volume=bind_volume,
            ),
            {},
            self.home,
            {},
        )

    def test_new_owner_keeps_uuid_through_completion(self) -> None:
        fd, token = state.begin_owner(self.home)
        try:
            state.complete_owner(self.home, fd, token)
            value = json.loads((self.home / state.WITNESS).read_bytes())
            self.assertEqual(
                (value["state"], value["volume_uuid"]), ("complete", VOLUME)
            )
        finally:
            os.close(fd)

    def test_bound_completion_survives_device_change_without_boot(
        self,
    ) -> None:
        self.change_device()
        with mock.patch.object(
            pause.review_boot_identity,
            "observe",
            side_effect=AssertionError("unnecessary boot check"),
        ):
            fd, token = state.begin_owner(self.home)
            try:
                state.complete_owner(self.home, fd, token)
            finally:
                os.close(fd)
            self.assertEqual(self.recover(confirmed=False)[0], 0)

    def test_wrong_uuid_refuses_even_with_matching_device(self) -> None:
        retained = (self.home / state.WITNESS).read_bytes()
        with mock.patch(
            "muninn.review_volume_identity.observe", return_value=OTHER_VOLUME
        ):
            with self.assertRaisesRegex(ValueError, "pause_unverified"):
                state.begin_owner(self.home)
            self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), retained)
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_wrong_inode_refuses_ordinary_entry(self) -> None:
        path = self.home / state.WITNESS
        value = json.loads(path.read_bytes())
        value["lock_identity"][1] += 1
        store.write_json_atomic(path, value)
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.begin_owner(self.home)
        with (
            self.assertRaisesRegex(ValueError, "pause_unverified"),
            pause.erase_connection(
                self.home, wait_s=0, status=pause.EraseStatus()
            ),
        ):
            self.fail("different lock inode admitted")

    def test_legacy_completion_requires_confirmation_without_mutation(
        self,
    ) -> None:
        self.change_device(legacy=True)
        self.baseline()
        before = {
            name: (self.home / name).read_bytes()
            for name in (state.WITNESS, state.BASELINE)
        }
        code, result = self.recover(confirmed=False)
        self.assertEqual(code, 2)
        assert isinstance(result, dict)
        self.assertEqual(
            result["reason"], "legacy_volume_confirmation_required"
        )
        self.assertEqual(
            before, {name: (self.home / name).read_bytes() for name in before}
        )

    def test_confirmed_legacy_completion_replays_intent_in_same_boot(
        self,
    ) -> None:
        interrupt_erasure(self.home, "fixture-thread")
        self.change_device(legacy=True)
        self.baseline()
        code, result = self.recover(confirmed=True)
        self.assertEqual(code, 0, result)
        self.assertFalse((self.home / state.BASELINE).exists())
        value = json.loads((self.home / state.WITNESS).read_bytes())
        self.assertEqual(value["volume_uuid"], VOLUME)
        info = (self.home / state.LOCK).stat()
        self.assertEqual(value["lock_identity"], [info.st_dev, info.st_ino])
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
            )

    def test_confirmation_without_baseline_can_migrate_completion(
        self,
    ) -> None:
        self.change_device(legacy=True)
        self.assertEqual(self.recover(confirmed=True)[0], 0)

    def test_changed_baseline_refuses_without_overwrite(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        path = self.home / state.BASELINE
        value = json.loads(path.read_bytes())
        value["predecessor"]["sha256"] = "0" * 64
        store.write_json_atomic(path, value)
        retained = path.read_bytes()
        self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertEqual(path.read_bytes(), retained)

    def test_confirmation_cannot_override_baseline_volume(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        with mock.patch(
            "muninn.review_volume_identity.observe", return_value=OTHER_VOLUME
        ):
            self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertTrue((self.home / state.BASELINE).exists())

    def test_changed_witness_during_replay_cannot_be_overwritten(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        replay = pause.reapply_tombstones
        path = self.home / state.WITNESS
        retained = path.read_bytes() + b" "

        def changed(conn: sqlite3.Connection, home: Path) -> int:
            count = replay(conn, home)
            path.write_bytes(retained)
            return count

        with mock.patch.object(
            pause, "reapply_tombstones", side_effect=changed
        ):
            self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertEqual(path.read_bytes(), retained)
        self.assertTrue((self.home / state.BASELINE).exists())

    def test_interrupted_publication_retains_repair_authority(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        publish = state.publish

        def interrupted(
            home: Path, fd: int, name: str, value: state.StrictModel
        ) -> None:
            publish(home, fd, name, value)
            raise OSError("publication sync failure")

        with (
            mock.patch.object(state, "publish", side_effect=interrupted),
            self.assertRaises(OSError),
        ):
            self.recover(confirmed=True)
        self.assertTrue((self.home / state.BASELINE).exists())
        self.assertEqual(self.recover(confirmed=False)[0], 0)

    def test_confirmation_does_not_complete_active_owner(self) -> None:
        fd, _ = state.begin_owner(self.home)
        os.close(fd)
        code, result = self.recover(confirmed=True)
        self.assertEqual(code, 2)
        assert isinstance(result, dict)
        self.assertEqual(result["reason"], "reboot_required")

    def test_wrong_uuid_on_active_owner_never_seeds_baseline(self) -> None:
        fd, _ = state.begin_owner(self.home)
        os.close(fd)
        with mock.patch(
            "muninn.review_volume_identity.observe", return_value=OTHER_VOLUME
        ):
            self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_installer_preparation_keeps_legacy_readable_record(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        self.assertEqual(self.recover(confirmed=True, bind_volume=False)[0], 0)
        value = json.loads((self.home / state.WITNESS).read_bytes())
        self.assertEqual(
            set(value), {"version", "lock_identity", "owner", "state"}
        )
        fd, token = state.begin_owner(self.home)
        try:
            state.complete_owner(self.home, fd, token)
        finally:
            os.close(fd)
        self.assertIn(
            "volume_uuid", json.loads((self.home / state.WITNESS).read_bytes())
        )

    def test_volume_field_is_strict_and_legacy_records_still_parse(
        self,
    ) -> None:
        value = json.loads((self.home / state.WITNESS).read_bytes())
        for invalid in (
            42,
            True,
            "invalid",
            "00000000-0000-0000-0000-000000000000",
        ):
            self.assertIsNone(
                state.parse(
                    json.dumps(value | {"volume_uuid": invalid}).encode(),
                    state.Witness,
                )
            )
        value.pop("volume_uuid")
        self.assertIsNotNone(
            state.parse(json.dumps(value).encode(), state.Witness)
        )

    def test_volume_change_during_replay_cannot_be_bound(self) -> None:
        self.change_device(legacy=True)
        self.baseline()
        path = self.home / state.WITNESS
        retained = path.read_bytes()
        with (
            mock.patch.object(pause, "reapply_tombstones"),
            mock.patch(
                "muninn.review_volume_identity.observe",
                side_effect=[VOLUME, VOLUME, OTHER_VOLUME],
            ),
        ):
            self.assertEqual(self.recover(confirmed=True)[0], 2)
        self.assertEqual(path.read_bytes(), retained)
        self.assertTrue((self.home / state.BASELINE).exists())
