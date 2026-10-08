"""Preserve uncertain SQLite state and retain rollback evidence."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from install import snapshot
from muninn import platform_io


class SnapshotSafetyTests(unittest.TestCase):
    """Real private SQLite files exercise backup and restoration boundaries."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve()
        platform_io.ensure_private_dir(self.data)
        self.live = self.data / "muninn.sqlite"
        self.saved = self.data / "saved.sqlite"
        self.create(self.live, 2)
        self.create(self.saved, 1)

    def create(self, path: Path, version: int) -> None:
        """Create a private SQLite fixture with one committed row."""
        fd = platform_io.open_private(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        os.close(fd)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE t(x)")
            connection.execute("INSERT INTO t VALUES (?)", (version,))
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()

    def test_existing_backup_temporary_is_retained(self) -> None:
        temporary = self.data / "copy.tmp"
        temporary.write_bytes(b"unknown private copy")
        with self.assertRaises(FileExistsError):
            snapshot._copy(self.live, temporary, 2)
        self.assertEqual(temporary.read_bytes(), b"unknown private copy")

    def test_each_residual_sidecar_refuses_before_restore(self) -> None:
        before = self.live.read_bytes(), self.saved.read_bytes()
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = self.data / f"muninn.sqlite{suffix}"
                sidecar.write_bytes(b"unknown transaction")
                with self.assertRaises(OSError):
                    snapshot._swap(self.data, self.saved)
                self.assertEqual(
                    (self.live.read_bytes(), self.saved.read_bytes()), before
                )
                self.assertEqual(sidecar.read_bytes(), b"unknown transaction")
                sidecar.unlink()

    def test_uncertain_publication_retains_original_snapshot(self) -> None:
        real = snapshot.publish
        original = self.saved.read_bytes()

        def uncertain(source: Path, target: Path, **kwargs: bool) -> None:
            real(source, target, **kwargs)
            raise OSError("synthetic durability uncertainty")

        with (
            mock.patch.object(snapshot, "publish", uncertain),
            self.assertRaises(OSError),
        ):
            snapshot._swap(self.data, self.saved)
        self.assertTrue(self.saved.exists())
        self.assertEqual(self.saved.read_bytes(), original)
        self.assertEqual(snapshot.store_version(self.live), 1)

    def test_validated_descriptor_remains_open_during_sqlite_access(
        self,
    ) -> None:
        opened: list[BinaryIO] = []
        real_open = platform_io.open_regular
        real_connect = sqlite3.connect

        def opening(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            handle = real_open(path, root=root, expected=expected)
            opened.append(handle)
            return handle

        def connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
            self.assertTrue(any(not handle.closed for handle in opened))
            return real_connect(database, uri=uri)

        with (
            mock.patch.object(platform_io, "open_regular", opening),
            mock.patch.object(sqlite3, "connect", connect),
        ):
            self.assertEqual(snapshot.store_version(self.live), 2)

    def test_unsafe_source_refuses_before_creating_a_copy(self) -> None:
        temporary = self.data / "copy.tmp"
        with (
            mock.patch.object(
                snapshot.snapshot_io,
                "_private",
                side_effect=PermissionError("unsafe"),
            ),
            self.assertRaises(PermissionError),
        ):
            snapshot._copy(self.live, temporary, 2)
        self.assertFalse(temporary.exists())

    def test_source_identity_change_refuses_after_sqlite_read(self) -> None:
        real = snapshot.snapshot_io._unchanged
        calls = 0

        def changed(identities: list[tuple[Path, os.stat_result]]) -> None:
            nonlocal calls
            calls += 1
            real(identities)
            if calls == 2:
                raise OSError("SQLite source changed identity")

        with (
            mock.patch.object(snapshot.snapshot_io, "_unchanged", changed),
            self.assertRaisesRegex(OSError, "changed identity"),
        ):
            snapshot.store_version(self.live)
        self.assertEqual(calls, 2)

    def test_foreign_or_public_source_metadata_is_not_repaired(self) -> None:
        before = self.live.stat()
        if sys.platform == "win32":
            with (
                mock.patch.object(
                    snapshot.snapshot_io.platform_windows,
                    "assert_private",
                    side_effect=PermissionError("unsafe ACL"),
                ),
                self.assertRaises(PermissionError),
            ):
                snapshot.store_version(self.live)
        else:
            self.live.chmod(0o644)
            with self.assertRaises(PermissionError):
                snapshot.store_version(self.live)
            self.assertEqual(self.live.stat().st_mode & 0o777, 0o644)
            self.live.chmod(0o600)
        self.assertEqual(self.live.stat().st_ino, before.st_ino)

    def test_restore_cleanup_uses_only_supported_directory_sync(self) -> None:
        def move(source: Path, target: Path) -> None:
            source.replace(target)

        with (
            mock.patch.object(snapshot, "publish", move),
            mock.patch.object(snapshot.file_sync, "sync_path") as sync,
        ):
            snapshot._swap(self.data, self.saved)
        if sys.platform == "win32":
            sync.assert_not_called()
        else:
            sync.assert_called_once_with(self.data)

    def test_healthy_restore_publishes_then_removes_original(self) -> None:
        snapshot._swap(self.data, self.saved)
        self.assertFalse(self.saved.exists())
        self.assertEqual(snapshot.store_version(self.live), 1)
