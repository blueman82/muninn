"""Check the strict input boundary of the unqualified Windows boot probe."""

from __future__ import annotations

import sys
import unittest

from tests import windows_recovery_probe as probe


class WindowsRecoveryProbeTests(unittest.TestCase):
    """Reject ambiguous observations before they can enter native evidence."""

    def test_boot_counter_requires_a_dword_and_exact_integer(self) -> None:
        for value in (True, False, -1, 2**32, "12", b"12", None):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "boot_counter_invalid"),
            ):
                probe.validate_counter(value, 4)
        for kind in (1, 3, 7, 11):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(ValueError, "boot_counter_invalid"),
            ):
                probe.validate_counter(12, kind)

    def test_dword_endpoints_remain_exact(self) -> None:
        self.assertEqual(probe.validate_counter(0, 4), 0)
        self.assertEqual(probe.validate_counter(2**32 - 1, 4), 4294967295)

    @unittest.skipIf(sys.platform == "win32", "native host reads real state")
    def test_non_windows_host_never_emits_a_boot_observation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "windows_host_required"):
            probe.observe()

    def test_only_nonempty_identical_exact_counters_are_stable(self) -> None:
        cases: list[list[int]] = [
            [],
            [12, 13],
            [13, 12],
            [0, 4294967295],
            [True],
        ]
        for samples in cases:
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                probe.verify_samples(samples)
        self.assertEqual(probe.verify_samples([12, 12, 12]), 12)

    def test_probe_failures_report_finite_actionable_codes(self) -> None:
        errors = (
            (FileNotFoundError("private text"), "boot_counter_absent"),
            (PermissionError("private text"), "boot_counter_access_denied"),
            (ValueError("private text"), "boot_counter_invalid"),
            (ValueError("boot_counter_unstable"), "boot_counter_unstable"),
            (RuntimeError("private text"), "windows_host_required"),
            (OSError("private text"), "native_registry_unavailable"),
        )
        for error, expected in errors:
            with self.subTest(expected=expected):
                self.assertEqual(probe.failure_code(error), expected)
