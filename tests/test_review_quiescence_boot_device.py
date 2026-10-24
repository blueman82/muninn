"""Exercise reboot repair when a macOS filesystem device number changes."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from muninn import review_quiescence as quiescence
from muninn import review_quiescence_state as state
from muninn import store
from tests.test_review_quiescence import QuiescenceCase

BOOT_A = "darwin:12345678-1234-4234-8234-123456789abc"
BOOT_B = "darwin:87654321-1234-4234-8234-123456789abc"
VOLUME_A = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"
VOLUME_B = "1c78c237-1f57-4ada-acac-c2e7630192c8"


@unittest.skipIf(os.name == "nt", "Windows repair retirement is unsupported")
class QuiescenceBootDeviceTests(QuiescenceCase):
    """Keep recovery bound to unchanged private files across a new boot."""

    def setUp(self) -> None:
        """Give each simulated boot a selectable native volume UUID."""
        super().setUp()
        self.volume_uuid = VOLUME_A
        patcher = mock.patch.object(
            quiescence.review_volume_identity,
            "observe",
            side_effect=lambda home: self.volume_uuid,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def remap_baseline(
        self, *, lock: bool = True, witness: bool = True
    ) -> None:
        """Simulate a prior device number without changing file bytes."""
        path = self.home / state.BASELINE
        value = json.loads(path.read_text())
        if lock:
            value["lock_identity"][0] += 1
        if witness:
            value["predecessor"]["identity"][0] += 1
        store.write_json_atomic(path, value)

    def recover(self, boot: str) -> None:
        """Attempt one guarded recovery with an observed boot identity."""
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=boot
            ),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ) as conn,
        ):
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                1,
            )

    def test_later_boot_reconciles_device_remap_with_same_files(self) -> None:
        """A new boot should require the recorded persistent volume UUID."""
        self.abandon()
        self.refuse(BOOT_A)
        baseline = state.parse(
            state.inspect(self.home, state.BASELINE)[0], state.Baseline
        )
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(baseline.volume_uuid, VOLUME_A)
        self.remap_baseline()
        self.recover(BOOT_B)
        self.assertEqual(self.status.repair_phase, "complete")
        self.assertFalse((self.home / state.BASELINE).exists())
        witness = state.parse(
            state.inspect(self.home, state.WITNESS)[0], state.Witness
        )
        self.assertIsNotNone(witness)
        assert witness is not None
        self.assertEqual(witness.state, "complete")

    def test_later_boot_refuses_changed_volume_uuid(self) -> None:
        """Matching file identities do not prove volume identity."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        self.volume_uuid = VOLUME_B
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("changed volume admitted")
        self.assertEqual(self.status.repair_phase, "baseline_recorded")

    def test_later_boot_refuses_changed_uuid_with_same_device(self) -> None:
        """A reused device number cannot bypass a recorded volume UUID."""
        self.abandon()
        self.refuse(BOOT_A)
        self.volume_uuid = VOLUME_B
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("changed volume admitted with reused device number")
        self.assertEqual(self.status.repair_phase, "baseline_recorded")

    def test_legacy_baseline_cannot_authorize_device_remap(self) -> None:
        """An old checkpoint cannot borrow today's volume UUID."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        path = self.home / state.BASELINE
        value = json.loads(path.read_text())
        del value["volume_uuid"]
        store.write_json_atomic(path, value)
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("legacy baseline admitted")

    def test_missing_volume_uuid_refuses_new_baseline(self) -> None:
        """No native UUID must leave no false repair authority."""
        self.abandon()
        with (
            mock.patch.object(
                quiescence.review_volume_identity,
                "observe",
                side_effect=state.QuiescenceRefusedError(
                    "quiescence_unverified"
                ),
            ),
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_A
            ),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("baseline recorded without volume UUID")
        self.assertFalse((self.home / state.BASELINE).exists())

    def test_same_boot_still_refuses_device_remap(self) -> None:
        """A changed device number does not shorten the two-boot rule."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_A
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("same boot admitted")
        self.assertEqual(self.status.repair_phase, "baseline_recorded")

    def test_later_boot_refuses_witness_change_with_device_remap(self) -> None:
        """A byte change must keep the uncertain predecessor fenced."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        path = self.home / state.WITNESS
        path.write_bytes(path.read_bytes() + b" ")
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("changed witness admitted")

    def test_later_boot_refuses_incoherent_device_remap(self) -> None:
        """The lock and witness must share each boot's device number."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline(lock=False)
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("incoherent baseline admitted")

    def test_later_boot_refuses_replaced_lock_with_device_remap(self) -> None:
        """A matching device change cannot hide a different lock inode."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        path = self.home / state.LOCK
        previous_inode = path.stat().st_ino
        path.rename(self.home / "old-lock")
        path.touch(mode=0o600)
        self.assertNotEqual(path.stat().st_ino, previous_inode)
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("replaced lock admitted")

    def test_later_boot_refuses_replaced_witness_with_same_bytes(self) -> None:
        """An identical copy at a new inode is still a new witness file."""
        self.abandon()
        self.refuse(BOOT_A)
        self.remap_baseline()
        path = self.home / state.WITNESS
        previous_inode = path.stat().st_ino
        raw = path.read_bytes()
        path.rename(self.home / "old-witness")
        path.write_bytes(raw)
        path.chmod(0o600)
        self.assertNotEqual(path.stat().st_ino, previous_inode)
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("replaced witness admitted")

    def test_linux_later_boot_keeps_full_device_identity(self) -> None:
        """The macOS exception must not alter another platform's repair."""
        linux_a = BOOT_A.replace("darwin:", "linux:")
        linux_b = BOOT_B.replace("darwin:", "linux:")
        self.abandon()
        self.refuse(linux_a)
        self.remap_baseline()
        with (
            mock.patch.object(
                quiescence.review_boot_identity,
                "observe",
                return_value=linux_b,
            ),
            self.assertRaisesRegex(ValueError, "reboot_required"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("Linux device remap admitted")
