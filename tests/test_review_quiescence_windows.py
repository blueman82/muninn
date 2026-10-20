"""Verify Windows metadata without weakening quiescence drift checks."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from muninn import review_quiescence_state as state


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
                    self.assertRaisesRegex(
                        ValueError, "quiescence_unverified"
                    ),
                ):
                    self.inspect(changed=changed, field=field)
