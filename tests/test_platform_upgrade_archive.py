"""Retain upgrade source bytes without changing live state or erasure keys."""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Literal
from unittest import mock

from muninn import erase_residue, platform_io, platform_rebuild, store
from tests.store_support import StoreCase


class UpgradeArchiveTests(StoreCase):
    """Exercise private disposable archives through the upgrade interface."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.home.resolve()
        self.db = store.db_path(self.home)
        store.ensure_private_dir(self.home)
        self.write(self.db, b"original old knowledge")
        self.key = self.home / "tombstone.key"
        self.write(self.key, b"synthetic durable key")
        self.identities = (self.db.stat().st_ino, self.key.stat().st_ino)

    def write(self, path: Path, data: bytes) -> None:
        """Create fixture bytes with private exclusive descriptor ownership."""
        fd = platform_io.open_private(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        with os.fdopen(fd, "wb") as output:
            output.write(data)

    def retain(self) -> Path:
        """Require the public archive operation before checking effects."""
        self.assertTrue(hasattr(platform_rebuild, "retain_upgrade_store"))
        return platform_rebuild.retain_upgrade_store(self.home, self.db)

    def assert_untouched(self) -> None:
        """Require live source and key content, identity and privacy."""
        self.assertEqual(self.db.read_bytes(), b"original old knowledge")
        self.assertEqual(self.key.read_bytes(), b"synthetic durable key")
        self.assertEqual(
            (self.db.stat().st_ino, self.key.stat().st_ino), self.identities
        )
        self.assertTrue(platform_io.is_private(self.db))
        self.assertTrue(platform_io.is_private(self.key))

    def test_fixed_upgrade_prefix_private_unique_bytes_and_real_residue(
        self,
    ) -> None:
        first, second = self.retain(), self.retain()
        self.assertNotEqual(first, second)
        for archive in (first, second):
            self.assertTrue(
                archive.name.startswith("muninn.sqlite.pre-upgrade-")
            )
            self.assertEqual(archive.read_bytes(), b"original old knowledge")
            self.assertNotEqual(archive.stat().st_ino, self.identities[0])
            self.assertTrue(platform_io.is_private(archive))
        self.assertEqual(
            erase_residue.aside_files(self.home),
            sorted([str(first), str(second)]),
        )
        self.assert_untouched()

    def test_partial_write_failure_preserves_enumerable_bytes(self) -> None:
        failure = OSError("synthetic partial write")

        def copy(source: BinaryIO, output: BinaryIO) -> None:
            output.write(source.read(8))
            output.flush()
            raise failure

        with (
            mock.patch.object(platform_rebuild.shutil, "copyfileobj", copy),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        paths = erase_residue.aside_files(self.home)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), b"original")
        self.assert_untouched()

    def test_collision_is_exclusive_without_retry_or_live_mutation(
        self,
    ) -> None:
        with mock.patch.object(
            platform_rebuild.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="fixed"),
        ):
            archive = self.retain()
            with self.assertRaises(FileExistsError):
                self.retain()
        self.assertEqual(archive.read_bytes(), b"original old knowledge")
        expected = [str(archive)]
        if sys.platform == "win32":
            partial = archive.with_name(f"{archive.name}.partial")
            self.assertEqual(partial.read_bytes(), b"original old knowledge")
            self.assertTrue(platform_io.is_private(partial))
            expected.append(str(partial))
        self.assertEqual(erase_residue.aside_files(self.home), expected)
        self.assert_untouched()

    def test_write_sync_and_close_failures_keep_private_enumerable_artifacts(
        self,
    ) -> None:
        real_open, real_close = os.fdopen, os.close
        failure = OSError("archive operation failed")

        def write_failure(
            fd: int, mode: Literal["rb", "wb"], *, closefd: bool = True
        ) -> BinaryIO:
            handle = real_open(fd, mode, closefd=closefd)
            if mode != "wb":
                return handle
            wrapped = mock.Mock(wraps=handle)
            wrapped.write.side_effect = failure
            return wrapped

        def close_failure(fd: int) -> None:
            real_close(fd)
            raise failure

        for name, target in (
            ("write", mock.patch.object(os, "fdopen", write_failure)),
            (
                "sync",
                mock.patch.object(
                    platform_rebuild, "sync_fd", side_effect=failure
                ),
            ),
            ("close", mock.patch.object(os, "close", close_failure)),
        ):
            before = set(erase_residue.aside_files(self.home))
            with (
                self.subTest(name=name),
                target,
                self.assertRaises(OSError) as caught,
            ):
                self.retain()
            self.assertIs(caught.exception, failure)
            added = set(erase_residue.aside_files(self.home)) - before
            self.assertEqual(len(added), 1)
            artifact = Path(added.pop())
            self.assertTrue(
                artifact.name.startswith("muninn.sqlite.pre-upgrade-")
            )
            self.assertTrue(platform_io.is_private(artifact))
            self.assertEqual(
                artifact.read_bytes(),
                b"" if name == "write" else b"original old knowledge",
            )
            self.assert_untouched()

    def test_copy_error_wins_over_secondary_descriptor_close_failure(
        self,
    ) -> None:
        failure = OSError("original copy failure")
        original_close = os.close

        def close(fd: int) -> None:
            original_close(fd)
            raise OSError("secondary close failure")

        with (
            mock.patch.object(
                platform_rebuild.shutil, "copyfileobj", side_effect=failure
            ),
            mock.patch.object(os, "close", close),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(erase_residue.aside_files(self.home)), 1)
        self.assert_untouched()

    @unittest.skipIf(sys.platform == "win32", "POSIX parent descriptor")
    def test_parent_sync_error_wins_over_close_and_retains_complete_copy(
        self,
    ) -> None:
        failure = OSError("original directory sync failure")
        original_sync, original_close = platform_rebuild.sync_fd, os.close

        def sync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise failure
            original_sync(fd)

        def close(fd: int) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            original_close(fd)
            if directory:
                raise OSError("secondary directory close failure")

        with (
            mock.patch.object(platform_rebuild, "sync_fd", sync),
            mock.patch.object(os, "close", close),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        paths = erase_residue.aside_files(self.home)
        self.assertEqual(len(paths), 1)
        self.assertEqual(
            Path(paths[0]).read_bytes(), b"original old knowledge"
        )
        self.assert_untouched()

    @unittest.skipIf(sys.platform == "win32", "fake Windows branch on POSIX")
    def test_native_archive_failure_before_and_after_move_never_restores(
        self,
    ) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved):
                self.native_failure(moved=moved)

    def native_failure(self, *, moved: bool) -> None:
        """Inject a native publication fault around a real file move."""
        calls: list[tuple[Path, Path, bool, bool]] = []
        failure = OSError("native archive publication failed")
        before = set(erase_residue.aside_files(self.home))

        def move(
            source: Path, target: Path, *, replace: bool, retry_move: bool
        ) -> None:
            calls.append((source, target, replace, retry_move))
            if moved:
                source.replace(target)
            raise failure

        with (
            mock.patch.object(
                platform_rebuild, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(
                platform_rebuild.platform_windows,
                "assert_private",
                create=True,
            ),
            mock.patch.object(
                platform_rebuild.platform_windows, "publish", move, create=True
            ),
            mock.patch.object(
                platform_rebuild, "_sync_parent", side_effect=AssertionError
            ),
            mock.patch.object(Path, "unlink", side_effect=AssertionError),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 1)
        source, target, replace, retry = calls[0]
        self.assertEqual(source, target.with_name(f"{target.name}.partial"))
        self.assertTrue(target.name.startswith("muninn.sqlite.pre-upgrade-"))
        self.assertEqual((replace, retry), (False, False))
        self.assertEqual(
            set(erase_residue.aside_files(self.home)) - before,
            {str(target if moved else source)},
        )
        self.assertEqual(
            (target if moved else source).read_bytes(),
            b"original old knowledge",
        )
        self.assert_untouched()

    @unittest.skipIf(sys.platform == "win32", "POSIX unsafe mode and link")
    def test_unsafe_source_is_refused_without_repair_or_archive_creation(
        self,
    ) -> None:
        self.db.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.retain()
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o644)
        self.db.chmod(0o600)
        linked = self.home / "linked"
        linked.symlink_to(self.db)
        with self.assertRaises(OSError):
            platform_rebuild.retain_upgrade_store(self.home, linked)
        self.assertEqual(erase_residue.aside_files(self.home), [])
        self.assert_untouched()
