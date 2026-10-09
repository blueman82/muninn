"""File sync guarantees and error propagation for runtime writers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import file_sync


class FileSyncTests(unittest.TestCase):
    """Sync an open descriptor without swallowing a failed fallback."""

    def test_real_file_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data"
            with path.open("wb") as handle:
                handle.write(b"synthetic\n")
                handle.flush()
                file_sync.sync_fd(handle.fileno())
            self.assertEqual(path.read_bytes(), b"synthetic\n")

    def test_fsync_error_propagates(self) -> None:
        with (
            mock.patch.object(file_sync, "_full_fsync", return_value=False),
            mock.patch.object(os, "fsync", side_effect=OSError("disk")),
            self.assertRaisesRegex(OSError, "disk"),
        ):
            file_sync.sync_fd(123)

    def test_full_sync_avoids_a_second_flush(self) -> None:
        with (
            mock.patch.object(file_sync, "_full_fsync", return_value=True),
            mock.patch.object(os, "fsync") as flush,
        ):
            file_sync.sync_fd(123)
        flush.assert_not_called()

    def test_fallback_flushes_the_same_descriptor(self) -> None:
        with (
            mock.patch.object(file_sync, "_full_fsync", return_value=False),
            mock.patch.object(os, "fsync") as flush,
        ):
            file_sync.sync_fd(123)
        flush.assert_called_once_with(123)
