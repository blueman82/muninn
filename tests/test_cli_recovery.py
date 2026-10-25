"""Recover an interrupted maintenance pause through the supported CLI."""

from __future__ import annotations

import json
import os
import sys
import unittest
from argparse import Namespace
from contextlib import closing
from unittest import mock

from muninn import cli_recovery, review_pause, store, tombstones
from muninn import review_pause_state as state
from muninn.cli_parser import build_parser
from tests.cli_support import CliCase
from tests.review_pause_support import initialize, interrupt_erasure
from tests.store_support import Child

BOOT_A = "linux:12345678-1234-4234-8234-123456789abc"
BOOT_B = "linux:87654321-1234-4234-8234-123456789abc"
DARWIN_BOOT_A = "darwin:12345678-1234-4234-8234-123456789abc"
DARWIN_BOOT_B = "darwin:87654321-1234-4234-8234-123456789abc"
VOLUME_UUID = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"


class CliRecoveryTests(CliCase):
    """Keep automated recovery bounded by existing proof checks."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)

    def test_recover_parser_accepts_legacy_volume_confirmation(self) -> None:
        """Expose the explicit confirmation for direct recovery calls."""
        args = build_parser().parse_args(
            ["recover", "--confirm-legacy-volume"]
        )
        self.assertTrue(args.confirm_legacy_volume)

    def leave_uncertain_owner(self) -> None:
        """Model a process that ended before it completed its pause record."""
        fd, _ = state.begin_owner(self.home)
        os.close(fd)

    def leave_legacy_device_remap(self) -> tuple[bytes, bytes]:
        """Model an old record whose device numbers changed after reboot."""
        self.leave_uncertain_owner()
        _, lock_file = state.inspect(self.home, state.LOCK)
        _, witness_file = state.inspect(self.home, state.WITNESS)
        assert lock_file is not None and witness_file is not None
        fd = state.acquire(self.home)
        try:
            state.publish(
                self.home,
                fd,
                state.BASELINE,
                state.Baseline(
                    version=1,
                    lock_identity=(
                        lock_file.identity[0] + 1,
                        lock_file.identity[1],
                    ),
                    boot=DARWIN_BOOT_A,
                    predecessor=state.Fingerprint(
                        identity=(
                            witness_file.identity[0] + 1,
                            witness_file.identity[1],
                        ),
                        sha256=witness_file.sha256,
                    ),
                ),
            )
        finally:
            os.close(fd)
        return (
            (self.home / state.BASELINE).read_bytes(),
            (self.home / state.WITNESS).read_bytes(),
        )

    @unittest.skipIf(
        sys.platform == "win32", "Windows recovery is unqualified"
    )
    def test_legacy_device_remap_requires_confirmation_without_changes(
        self,
    ) -> None:
        """Request approval before changing an old disk-unbound record."""
        baseline, witness = self.leave_legacy_device_remap()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="darwin"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=DARWIN_BOOT_B,
            ),
            mock.patch.object(
                review_pause.review_volume_identity,
                "observe",
                return_value=VOLUME_UUID,
            ),
        ):
            code, result = cli_recovery.recover(Namespace(), {}, self.home, {})
        assert result is not None
        self.assertEqual(
            (code, result["reason"]),
            (2, "legacy_volume_confirmation_required"),
        )
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), witness)

    @unittest.skipIf(
        sys.platform == "win32", "Windows recovery is unqualified"
    )
    def test_confirmed_legacy_device_remap_recovers(self) -> None:
        """Rebind a matching old record and complete normal recovery."""
        self.leave_legacy_device_remap()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="darwin"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=DARWIN_BOOT_B,
            ),
            mock.patch.object(
                review_pause.review_volume_identity,
                "observe",
                return_value=VOLUME_UUID,
            ),
        ):
            code, result = cli_recovery.recover(
                Namespace(confirm_legacy_volume=True), {}, self.home, {}
            )
        assert result is not None
        self.assertEqual(code, 0, result)
        self.assertEqual(result["recovery"]["status"], "recovered")
        self.assertFalse((self.home / state.BASELINE).exists())
        witness = json.loads((self.home / state.WITNESS).read_bytes())
        self.assertEqual(witness["state"], "complete")

    @unittest.skipIf(
        sys.platform == "win32", "Windows recovery is unqualified"
    )
    def test_confirmation_does_not_allow_changed_witness(self) -> None:
        """Keep the old baseline when witness content changed after capture."""
        baseline, _ = self.leave_legacy_device_remap()
        witness_path = self.home / state.WITNESS
        witness_path.write_bytes(witness_path.read_bytes() + b" ")
        changed_witness = witness_path.read_bytes()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="darwin"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=DARWIN_BOOT_B,
            ),
            mock.patch.object(
                review_pause.review_volume_identity,
                "observe",
                return_value=VOLUME_UUID,
            ),
        ):
            code, result = cli_recovery.recover(
                Namespace(confirm_legacy_volume=True), {}, self.home, {}
            )
        assert result is not None
        self.assertEqual((code, result["reason"]), (2, "pause_unverified"))
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)
        self.assertEqual(witness_path.read_bytes(), changed_witness)

    @unittest.skipIf(
        sys.platform == "win32",
        "Linux boot and file-identity repair cannot be emulated on Windows.",
    )
    def test_recover_waits_for_reboot_then_repairs_verified_linux_state(
        self,
    ) -> None:
        self.leave_uncertain_owner()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_A,
            ),
        ):
            code, first, _ = self.muninn("recover")
        self.assertEqual((code, first["reason"]), (2, "reboot_required"))
        self.assertIn("until the computer has rebooted", first["why"])
        self.assertIn("run `muninn recover` again", first["next_action"])
        self.assertTrue((self.home / state.BASELINE).is_file())
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_B,
            ),
        ):
            code, second, _ = self.muninn("recover")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["recovery"]["status"], "recovered")
        self.assertEqual(second["recovery"]["repair_phase"], "complete")
        self.assertIn(
            "system service manager", second["recovery"]["next_action"]
        )
        self.assertFalse((self.home / state.BASELINE).exists())
        witness = json.loads((self.home / state.WITNESS).read_text())
        self.assertEqual(witness["state"], "complete")

    def test_windows_refuses_without_changing_repair_evidence(self) -> None:
        self.leave_uncertain_owner()
        with (
            mock.patch.object(
                cli_recovery, "_platform_name", return_value="linux"
            ),
            mock.patch.object(
                review_pause.review_boot_identity,
                "observe",
                return_value=BOOT_A,
            ),
        ):
            self.muninn("recover")
        baseline = (self.home / state.BASELINE).read_bytes()
        witness = (self.home / state.WITNESS).read_bytes()
        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="win32"
        ):
            code, out, _ = self.muninn("recover")
        self.assertEqual(
            (code, out["reason"]), (2, "windows_repair_unqualified")
        )
        self.assertIn("not qualified", out["why"])
        self.assertIn("contact your Muninn maintainer", out["next_action"])
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), witness)

    def test_healthy_home_reports_no_recovery_needed(self) -> None:
        code, out, _ = self.muninn("recover")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["recovery"]["status"], "no_recovery_needed")

    def test_recover_applies_pending_erasure_with_complete_witness(
        self,
    ) -> None:
        """Replay durable intent before claiming there is no recovery."""
        interrupt_erasure(self.home, "fixture-thread")
        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="linux"
        ):
            code, result, _ = self.muninn("recover")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["recovery"]["status"], "recovered")
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0],
                1,
            )
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            tombstones.require_current_tombstones(conn, self.home)

    def test_windows_keeps_pending_erasure_unchanged(self) -> None:
        """Refuse Windows replay before changing its saved intent."""
        interrupt_erasure(self.home, "fixture-thread")
        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="win32"
        ):
            code, result, _ = self.muninn("recover")
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "windows_repair_unqualified")
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(ValueError, "erasure_pending"):
                tombstones.require_current_tombstones(conn, self.home)

    def test_empty_data_home_reports_no_recovery_needed(self) -> None:
        empty = self.tmp / "empty-home"
        empty.mkdir(mode=0o700)
        code, out, _ = self.muninn("recover", env={"MUNINN_HOME": str(empty)})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["recovery"]["status"], "no_recovery_needed")

    @unittest.skipIf(
        sys.platform == "win32",
        "Linux lock identity cannot be emulated on Windows.",
    )
    def test_complete_witness_retires_saved_baseline_as_recovery(self) -> None:
        """Report recovery when retry only needs to retire its baseline."""
        identity = state.inspect(self.home, state.LOCK)[1]
        assert identity is not None
        predecessor = state.inspect(self.home, state.WITNESS)[1]
        fd = state.acquire(self.home)
        try:
            state.publish(
                self.home,
                fd,
                state.BASELINE,
                state.Baseline(
                    version=1,
                    lock_identity=identity.identity,
                    boot=BOOT_A,
                    predecessor=predecessor,
                    volume_uuid=None,
                ),
            )
        finally:
            os.close(fd)

        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="linux"
        ):
            code, result, _ = self.muninn("recover")

        self.assertEqual(code, 0, result)
        self.assertEqual(result["recovery"]["status"], "recovered")
        self.assertEqual(result["recovery"]["repair_phase"], "complete")
        self.assertFalse((self.home / state.BASELINE).exists())

    @unittest.skipIf(
        sys.platform == "win32",
        "Linux managed-writer signaling cannot be emulated on Windows.",
    )
    def test_recover_reports_when_it_stops_a_managed_writer(self) -> None:
        """Tell the caller when recovery stopped a live managed writer."""
        child = Child(
            self,
            """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from muninn import poller_stop
from muninn import review_pause_state as state
home = Path(sys.argv[2])
fd, token = state.begin_owner(home)
print("ready", flush=True)
if not poller_stop.wait(home, os.getpid(), token, 10):
    raise RuntimeError("stop request was not received")
state.complete_owner(home, fd, token)
os.close(fd)
""",
            self.home,
        )
        child.wait_ready()

        with mock.patch.object(
            cli_recovery, "_platform_name", return_value="linux"
        ):
            code, result, _ = self.muninn("recover")
        stdout, stderr = child.proc.communicate(timeout=5)

        self.assertEqual((code, stdout, stderr), (0, "", ""))
        self.assertEqual(result["recovery"]["status"], "recovered")
        self.assertIn("stopped", result["recovery"]["message"])
        self.assertIn("doctor", result["recovery"]["next_action"])
