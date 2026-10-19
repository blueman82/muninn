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
from muninn import cli_rebuild, platform_io, platform_rebuild, store
from tests.cli_support import CliCase
from tests.review_quiescence_support import initialize


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


class RebuildSnapshotSafetyTests(CliCase):
    """Exercise snapshot safety through actual current replacement paths."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.db = store.db_path(self.home)

    def archives(self) -> list[Path]:
        """List retained copies without selecting automatic restore state."""
        return sorted(self.home.glob(store.RECOVERY_PREFIX + "*"))

    def test_existing_candidate_is_retained_without_resume(self) -> None:
        candidate = self.home / cli_rebuild.REBUILD
        candidate.write_bytes(b"unknown private copy")
        candidate.chmod(0o600)
        before = self.db.read_bytes()
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(
            (code, out["reason"]), (2, "rebuild_candidate_exists")
        )
        self.assertTrue(out["recovery_required"])
        self.assertTrue(out["publication_possible"])
        self.assertEqual(candidate.read_bytes(), b"unknown private copy")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.archives(), [])

    def test_each_residual_sidecar_refuses_before_material_access(
        self,
    ) -> None:
        before = self.db.read_bytes()
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = self.home / f"muninn.sqlite{suffix}"
                sidecar.write_bytes(b"unknown transaction")
                code, out, _ = self.muninn("rebuild")
                self.assertEqual((code, out["reason"]), (2, "source_changed"))
                self.assertEqual(self.db.read_bytes(), before)
                self.assertEqual(sidecar.read_bytes(), b"unknown transaction")
                self.assertEqual(self.archives(), [])
                sidecar.unlink()

    def test_uncertain_publication_retains_original_archive(self) -> None:
        publish = platform_rebuild.publish_replacement
        archive_bytes: list[bytes] = []
        original = self.db.stat().st_ino

        def uncertain(home: Path, db: Path, new: Path) -> None:
            archive_bytes.append(self.archives()[0].read_bytes())
            publish(home, db, new)
            raise OSError("synthetic durability uncertainty")

        with mock.patch.object(
            platform_rebuild, "publish_replacement", uncertain
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertTrue(out["publication_possible"])
        self.assertNotIn("old_restored", out)
        self.assertEqual(self.archives()[0].read_bytes(), archive_bytes[0])
        self.assertNotEqual(self.db.stat().st_ino, original)
        with closing(store.connect_ro(self.db)) as conn:
            store.assert_store_format(conn)

    def test_unsafe_source_refuses_before_creating_a_copy(self) -> None:
        with mock.patch.object(
            cli_rebuild.fingerprint,
            "capture",
            side_effect=cli_rebuild.fingerprint.SourceChangedError,
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertEqual(self.archives(), [])
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())

    def test_actual_publication_uses_supported_directory_sync(self) -> None:
        sync = platform_rebuild._sync_parent
        with mock.patch.object(
            platform_rebuild, "_sync_parent", wraps=sync
        ) as observed:
            code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        if sys.platform == "win32":
            observed.assert_not_called()
        else:
            self.assertEqual(
                observed.call_args_list,
                [mock.call(self.home), mock.call(self.home)],
            )

    def test_success_publishes_candidate_and_retains_fenced_original(
        self,
    ) -> None:
        original = self.db.stat().st_ino
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(self.db.stat().st_ino, original)
        self.assertEqual(len(self.archives()), 1)
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())
        with closing(store.connect_ro(self.archives()[0])) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
