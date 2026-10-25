"""Installer health checks try only the qualified recovery command."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import rollback
from install.context import Ctx, StepFailedError
from install.record import Record
from install.recovery_guidance import recover_pause
from install.verify import _doctor, verify


class DoctorRunner:
    """Return a failed health report, recovery success and a healthy report."""

    def __init__(self) -> None:
        """Prepare the three expected command results."""
        self.calls: list[list[str]] = []
        self.reports = [
            subprocess.CompletedProcess(
                [],
                1,
                b'{"recovery_guidance":{"status":"automatic_recovery_available"}}',
                b"",
            ),
            subprocess.CompletedProcess(
                [], 0, b'{"recovery":{"status":"recovered"}}', b""
            ),
            subprocess.CompletedProcess([], 0, b'{"checks":[]}', b""),
        ]

    def __call__(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Return the next result in the installer recovery sequence."""
        del env, input
        self.calls.append([str(value) for value in argv])
        return self.reports.pop(0)


class InstallerRecoveryTest(unittest.TestCase):
    """Keep installer recovery automatic only for checked repair guidance."""

    def test_recovered_pause_restarts_service_and_rechecks_doctor(
        self,
    ) -> None:
        """Retry health checks after the checked repair succeeds."""
        runner = DoctorRunner()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), runner, "test", say=messages.append)
            rec = Record()
            with mock.patch("install.verify.restart") as restart:
                result = _doctor(ctx, rec)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [call[-1] for call in runner.calls],
            ["doctor", "recover", "doctor"],
        )
        restart.assert_called_once_with(ctx, rec, registered=True)
        self.assertTrue(
            any(
                "recovered the interrupted pause automatically" in line
                for line in messages
            )
        )

    def test_upgrade_repairs_pause_before_preparing_store(self) -> None:
        """Continue the upgrade when checked recovery succeeds."""
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), DoctorRunner(), "test", say=messages.append)
            with (
                mock.patch(
                    "install.recovery_guidance.cli_recovery.recover",
                    return_value=(0, {"recovery": {"status": "recovered"}}),
                ) as recover,
            ):
                recover_pause(ctx, Record())

        recover.assert_called_once()
        self.assertTrue(
            any("upgrade will continue" in line for line in messages)
        )

    def test_windows_pause_refusal_keeps_service_and_data_safe(self) -> None:
        """Explain the manual boundary before the installer pauses service."""
        messages: list[str] = []
        record = Record()
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(
                Path(root),
                DoctorRunner(),
                "test",
                platform="win32",
                say=messages.append,
            )
            with (
                mock.patch(
                    "install.recovery_guidance.cli_recovery.recover",
                    return_value=(
                        2,
                        {
                            "reason": "windows_repair_unqualified",
                            "why": "Windows repair is not qualified.",
                            "next_action": "Keep the Muninn data folder "
                            "unchanged.",
                        },
                    ),
                ) as recover,
                self.assertRaises(StepFailedError),
            ):
                recover_pause(ctx, record)

        recover.assert_called_once()
        self.assertTrue(record["recovery_required"])
        self.assertTrue(
            any("Keep the Muninn data folder" in line for line in messages)
        )
        self.assertTrue(any("Health report:" in line for line in messages))

    def test_reboot_refusal_says_retry_the_installer(self) -> None:
        """Avoid making the user discover and run recovery by hand."""
        messages: list[str] = []
        record = Record()
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), DoctorRunner(), "test", say=messages.append)
            with (
                mock.patch(
                    "install.recovery_guidance.cli_recovery.recover",
                    return_value=(
                        2,
                        {
                            "reason": "reboot_required",
                            "why": "A reboot is required.",
                            "next_action": "Run muninn recover after reboot.",
                        },
                    ),
                ),
                self.assertRaises(StepFailedError),
            ):
                recover_pause(ctx, record)

        self.assertTrue(record["recovery_required"])
        self.assertTrue(
            any("rerun this same install command" in line for line in messages)
        )
        self.assertTrue(
            any("retry recovery automatically" in line for line in messages)
        )

    def test_upgrade_checks_incomplete_owner_without_repair_baseline(
        self,
    ) -> None:
        """Start recovery when an interrupted owner has no baseline yet."""
        messages: list[str] = []
        record = Record()
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), DoctorRunner(), "test", say=messages.append)
            with (
                mock.patch(
                    "install.recovery_guidance.cli_recovery.recover",
                    return_value=(
                        2,
                        {
                            "reason": "reboot_required",
                            "next_action": "Restart, then retry.",
                        },
                    ),
                ) as recover,
                self.assertRaises(StepFailedError),
            ):
                recover_pause(ctx, record)

        recover.assert_called_once()
        self.assertTrue(record["recovery_required"])

    def test_verification_checks_service_after_automatic_recovery(
        self,
    ) -> None:
        """Use fresh service state after the recovery restart completes."""
        recovered = False

        def recover_service(
            *_args: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal recovered
            recovered = True
            return subprocess.CompletedProcess([], 0, b'{"checks":[]}', b"")

        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), DoctorRunner(), "test", platform="darwin")
            rec = Record(python={"path": "/fixture/python"}, sha="fixture")
            with (
                mock.patch("install.verify.runtime.verify_runtime"),
                mock.patch("install.verify.job", return_value={"pid": 123}),
                mock.patch(
                    "install.verify.is_new", side_effect=lambda *_: recovered
                ),
                mock.patch(
                    "install.verify.fresh", side_effect=lambda *_: recovered
                ),
                mock.patch("install.verify.hook_invocations", return_value=[]),
                mock.patch("install.verify.ce.atomic_write"),
                mock.patch(
                    "install.verify._doctor", side_effect=recover_service
                ),
            ):
                verify(ctx, rec)

    def test_unresolved_pause_rollback_leaves_service_stopped(self) -> None:
        """Do not restart a service while pause recovery remains unresolved."""
        with tempfile.TemporaryDirectory() as root:
            ctx = Ctx(Path(root), DoctorRunner(), "test", upgrade=True)
            record = Record(upgrade=True, recovery_required=True)
            with (
                mock.patch("install.rollback.lifecycle.stop") as stop,
                mock.patch("install.rollback.start_again") as start,
                mock.patch("install.rollback.save"),
            ):
                rollback.rollback_owned(ctx, record)

        stop.assert_called_once_with(ctx)
        start.assert_not_called()
