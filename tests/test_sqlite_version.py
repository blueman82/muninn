"""Refuse SQLite versions with broken secure-delete integrity checking."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import store


class SQLiteVersionTests(unittest.TestCase):
    """An unsupported SQLite must not create or change store files."""

    def test_old_sqlite_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "missing"
            with (
                mock.patch.object(sqlite3, "sqlite_version_info", (3, 45, 1)),
                self.assertRaisesRegex(store.StoreUnavailableError, "3.46.1"),
            ):
                store.connect_rw(store.db_path(home))
            self.assertFalse(home.exists())

    def test_old_sqlite_keeps_existing_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "muninn.sqlite"
            path.write_bytes(b"untouched")
            before = path.stat().st_mode
            with (
                mock.patch.object(sqlite3, "sqlite_version_info", (3, 46, 0)),
                self.assertRaises(store.StoreUnavailableError),
            ):
                store.connect_rw(path)
            self.assertEqual(path.read_bytes(), b"untouched")
            self.assertEqual(path.stat().st_mode, before)
