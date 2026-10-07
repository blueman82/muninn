"""SQLite sidecars are checked before SQLite can replay private state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import store


class SidecarPrivacyTests(unittest.TestCase):
    """An independently unsafe journal is refused before any SQLite call."""

    def test_unsafe_sidecar_stops_before_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "muninn.sqlite"
            journal = db.with_name(db.name + "-journal")
            journal.write_bytes(b"synthetic original journal")
            with (
                mock.patch.object(store.os, "name", "nt"),
                mock.patch.object(store, "_ensure_dir"),
                mock.patch.object(
                    store.platform_io,
                    "is_private",
                    side_effect=lambda path, **kwargs: path != journal,
                ),
                mock.patch.object(store.sqlite3, "connect") as connect,
                self.assertRaises(PermissionError),
            ):
                store.connect_rw(db)
            connect.assert_not_called()
            self.assertFalse(db.exists())
            self.assertEqual(
                journal.read_bytes(), b"synthetic original journal"
            )
