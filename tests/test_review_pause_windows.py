"""Verify Windows metadata without weakening pause drift checks."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from muninn import review_pause_state as state
from muninn import review_repair_binding as repair_binding


class WindowsMetadataTests(unittest.TestCase):
    """Separate Windows creation time from descriptor change time."""

    def snapshot(self, *, descriptor: bool, **changes: int) -> os.stat_result:
        """Build one native-shaped record with distinct timestamp meanings."""
        fields = {
            "st_dev": 1,
            "st_ino": 2,
            "st_size": 7,
            "st_mtime_ns": 3,
            "st_birthtime_ns": 4,
            "st_ctime_ns": 5 if descriptor else 4,
            "st_mode": 0o100600,
            "st_uid": 0,
        }
        return cast(os.stat_result, SimpleNamespace(**(fields | changes)))

    def inspect(
        self, *, changed: str = "", field: str = "st_ctime_ns"
    ) -> bytes:
        """Read real bytes through controlled path and descriptor snapshots."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / state.WITNESS
            path.write_bytes(b"private")
            directory = home.stat()
            before = self.snapshot(descriptor=False)
            initial = self.snapshot(descriptor=True)
            after = self.snapshot(
                descriptor=True, **({field: 9} if changed == "fd" else {})
            )
            latest = self.snapshot(
                descriptor=False, **({field: 9} if changed == "path" else {})
            )
            with (
                path.open("rb") as handle,
                mock.patch.object(state.sys, "platform", "win32"),
                mock.patch.object(
                    state, "_private", side_effect=[directory, before, latest]
                ),
                mock.patch.object(
                    state.platform_io, "open_regular", return_value=handle
                ),
                mock.patch.object(state.platform_io, "assert_private_fd"),
                mock.patch.object(
                    state.os, "fstat", side_effect=[initial, after]
                ),
            ):
                raw, fingerprint = state.inspect(home, state.WITNESS)
            assert fingerprint is not None
            self.assertEqual(fingerprint.identity, (1, 2))
            self.assertEqual(
                fingerprint.sha256, hashlib.sha256(raw).hexdigest()
            )
            return raw

    def test_distinct_path_and_descriptor_ctime_remain_readable(self) -> None:
        self.assertEqual(self.inspect(), b"private")

    def test_changes_within_either_metadata_source_still_refuse(self) -> None:
        for changed in ("path", "fd"):
            for field in (
                "st_ctime_ns",
                "st_birthtime_ns",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_mode",
                "st_uid",
            ):
                with (
                    self.subTest(changed=changed, field=field),
                    self.assertRaisesRegex(ValueError, "pause_unverified"),
                ):
                    self.inspect(changed=changed, field=field)

    def test_legacy_confirmation_skips_non_darwin_pause_records(self) -> None:
        """Do not read protected lock records for a Darwin-only check."""
        baseline = state.Baseline(
            version=1,
            lock_identity=(1, 2),
            boot="linux:12345678-1234-4234-8234-123456789abc",
            predecessor=None,
        )
        for platform, raw, calls in (
            ("win32", b"", 0),
            ("darwin", b"", 1),
            ("darwin", baseline.model_dump_json().encode("utf-8"), 1),
        ):
            with (
                self.subTest(platform=platform, raw=raw),
                tempfile.TemporaryDirectory() as tmp,
                mock.patch.object(repair_binding.sys, "platform", platform),
                mock.patch.object(
                    state,
                    "inspect",
                    return_value=(raw, None),
                ) as inspect,
                mock.patch(
                    "muninn.review_repair_binding.review_boot_identity.observe",
                    side_effect=AssertionError("boot identity was read"),
                ),
            ):
                self.assertFalse(
                    repair_binding.legacy_volume_change_requires_confirmation(
                        Path(tmp)
                    )
                )
            self.assertEqual(inspect.call_count, calls)
            if calls:
                inspect.assert_called_with(Path(tmp), state.BASELINE)

    def test_matching_darwin_baseline_requires_confirmation(self) -> None:
        """Keep legacy confirmation active when Darwin evidence matches."""
        predecessor = state.Fingerprint(identity=(1, 3), sha256="a" * 64)
        baseline = state.Baseline(
            version=1,
            lock_identity=(1, 2),
            boot="darwin:12345678-1234-4234-8234-123456789abc",
            predecessor=predecessor,
        )
        witness = state.Witness(
            version=1,
            lock_identity=(3, 2),
            owner=state.Owner(pid=1, generation="b" * 32),
            state="active",
        )
        witness_file = state.Fingerprint(identity=(3, 3), sha256="a" * 64)
        lock_file = state.Fingerprint(identity=(3, 2), sha256="d" * 64)
        with (
            mock.patch.object(repair_binding.sys, "platform", "darwin"),
            mock.patch.object(
                state,
                "inspect",
                side_effect=[
                    (baseline.model_dump_json().encode("utf-8"), None),
                    (witness.model_dump_json().encode("utf-8"), witness_file),
                    (b"", lock_file),
                ],
            ),
            mock.patch.object(
                repair_binding.review_boot_identity,
                "observe",
                return_value="darwin:87654321-1234-4234-8234-123456789abc",
            ),
            mock.patch.object(
                repair_binding.review_volume_identity,
                "observe",
                return_value="87654321-1234-4234-8234-123456789abc",
            ) as observe_volume,
        ):
            self.assertTrue(
                repair_binding.legacy_volume_change_requires_confirmation(
                    Path()
                )
            )
        observe_volume.assert_called_once_with(Path())
