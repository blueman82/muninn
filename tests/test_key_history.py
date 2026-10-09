"""Unknown retained tombstone history forbids missing-key recreation."""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from muninn import platform_io, store, tombstone_key


class KeyHistoryTests(unittest.TestCase):
    """Absence is different from an unsafe or unreadable existing log."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "private"
        platform_io.ensure_private_dir(self.home)

    def test_unsafe_existing_history_forbids_key_creation(self) -> None:
        log = self.home / tombstone_key.TOMBSTONE_FILE
        log.write_bytes(b"retained history cannot be inspected")
        real_open = platform_io.open_regular

        def unreadable(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            if path.name == log.name:
                raise PermissionError("synthetic unsafe retained history")
            return real_open(path, root=root, expected=expected)

        with (
            contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn,
            mock.patch.object(platform_io, "open_regular", unreadable),
            self.assertRaises(tombstone_key.TombstoneKeyError),
        ):
            tombstone_key.key_for(conn)
        self.assertFalse((self.home / tombstone_key.KEY_FILE).exists())
        with mock.patch.object(platform_io, "open_regular", unreadable):
            self.assertEqual(tombstone_key.key_problem(self.home), "missing")

    def test_absent_history_allows_first_install_key(self) -> None:
        self.assertFalse(tombstone_key.log_has_keyed(self.home))
        with contextlib.closing(
            store.connect_rw(store.db_path(self.home))
        ) as conn:
            key = tombstone_key.key_for(conn)
        self.assertEqual(len(key), 32)
        self.assertTrue(
            platform_io.is_private(self.home / tombstone_key.KEY_FILE)
        )

    def test_dangling_log_link_is_not_absence(self) -> None:
        outside = self.home.parent / "missing-outside-history"
        log = self.home / tombstone_key.TOMBSTONE_FILE
        log.symlink_to(outside)
        with (
            contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn,
            self.assertRaises(tombstone_key.TombstoneKeyError),
        ):
            tombstone_key.key_for(conn)
        self.assertFalse((self.home / tombstone_key.KEY_FILE).exists())
        self.assertFalse(outside.exists())
