"""Retain real private archives and never undo uncertain publication."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from unittest import mock

from muninn import erase_residue, platform_io, store
from muninn import platform_rebuild as rebuild


class RetainedPublicationTests(unittest.TestCase):
    """Exercise bytes, identities and residue with disposable private files."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        platform_io.ensure_private_dir(self.home)
        self.db = self.write("muninn.sqlite", b"old governed content")
        self.new = self.write("candidate", b"new replayed content")
        self.before = self.db.stat()

    def write(self, name: str, data: bytes) -> Path:
        """Create a private fixture with exclusive pathname ownership."""
        path = self.home / name
        fd = platform_io.open_private(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        return path

    def retain(self) -> Path:
        """Require the public retain operation before exercising behavior."""
        self.assertTrue(hasattr(rebuild, "retain_store"))
        return rebuild.retain_store(self.home, self.db)

    def publish(self) -> None:
        """Require the public one-attempt publication operation."""
        self.assertTrue(hasattr(rebuild, "publish_replacement"))
        rebuild.publish_replacement(self.home, self.db, self.new)

    def assert_source(self) -> None:
        """Require the live source's original bytes, inode and privacy."""
        self.assertEqual(self.db.read_bytes(), b"old governed content")
        self.assertEqual(self.db.stat().st_ino, self.before.st_ino)
        self.assertTrue(platform_io.is_private(self.db))

    def test_copy_is_unique_private_enumerable_and_does_not_move_source(
        self,
    ) -> None:
        archive = self.retain()
        second = self.retain()
        self.assertNotEqual(archive, second)
        self.assert_source()
        self.assertEqual(archive.read_bytes(), self.db.read_bytes())
        self.assertNotEqual(archive.stat().st_ino, self.before.st_ino)
        self.assertTrue(platform_io.is_private(archive))
        self.assertTrue(archive.name.startswith(store.RECOVERY_PREFIX))
        self.assertEqual(
            set(erase_residue.aside_files(self.home)),
            {str(archive), str(second)},
        )

    def test_success_keeps_archive_and_replaces_with_candidate_identity(
        self,
    ) -> None:
        archive = self.retain()
        identity = self.new.stat().st_ino
        self.publish()
        self.assertEqual(self.db.read_bytes(), b"new replayed content")
        self.assertEqual(self.db.stat().st_ino, identity)
        self.assertFalse(self.new.exists())
        self.assertEqual(archive.read_bytes(), b"old governed content")
        self.assertTrue(platform_io.is_private(self.db))

    def test_archive_collision_never_overwrites_or_retries(self) -> None:
        with mock.patch.object(
            rebuild.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="fixed"),
        ) as token:
            archive = self.retain()
            with self.assertRaises(FileExistsError):
                self.retain()
        self.assertEqual(token.call_count, 2)
        self.assertEqual(archive.read_bytes(), b"old governed content")
        self.assert_source()

    def test_partial_copy_error_preserves_original_and_enumerable_bytes(
        self,
    ) -> None:
        failure = OSError("copy failed")

        def partial(source: BinaryIO, output: BinaryIO) -> None:
            output.write(source.read(4))
            output.flush()
            raise failure

        with (
            mock.patch.object(rebuild.shutil, "copyfileobj", partial),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        paths = erase_residue.aside_files(self.home)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), b"old ")
        self.assert_source()

    def test_copy_sync_failure_keeps_complete_residue_and_original_error(
        self,
    ) -> None:
        failure = OSError("copy sync failed")
        with (
            mock.patch.object(rebuild, "sync_fd", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(erase_residue.aside_files(self.home)), 1)
        self.assert_source()

    @unittest.skipIf(sys.platform == "win32", "POSIX parent sync")
    def test_archive_parent_sync_failure_keeps_residue(self) -> None:
        failure = OSError("parent sync failed")
        with (
            mock.patch.object(rebuild, "_sync_parent", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(erase_residue.aside_files(self.home)), 1)
        self.assert_source()

    @unittest.skipIf(sys.platform == "win32", "POSIX replacement seam")
    def test_replace_then_raise_never_restores_retries_or_deletes(
        self,
    ) -> None:
        archive = self.retain()
        original = os.replace
        failure = OSError("replacement uncertain")

        def replace(source: Path, target: Path) -> None:
            original(source, target)
            raise failure

        with (
            mock.patch.object(
                rebuild.os, "replace", side_effect=replace
            ) as move,
            mock.patch.object(
                Path, "unlink", side_effect=OSError("cleanup forbidden")
            ) as unlink,
            self.assertRaises(OSError) as caught,
        ):
            self.publish()
        self.assertIs(caught.exception, failure)
        move.assert_called_once_with(self.new, self.db)
        unlink.assert_not_called()
        self.assertEqual(self.db.read_bytes(), b"new replayed content")
        self.assertEqual(archive.read_bytes(), b"old governed content")

    @unittest.skipIf(sys.platform == "win32", "POSIX replacement seam")
    def test_post_replace_sync_failure_keeps_new_live_and_archive(
        self,
    ) -> None:
        archive = self.retain()
        failure = OSError("publication sync failed")
        with (
            mock.patch.object(rebuild, "_sync_parent", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            self.publish()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.db.read_bytes(), b"new replayed content")
        self.assertEqual(archive.read_bytes(), b"old governed content")

    def test_candidate_sync_failure_prevents_replacement(self) -> None:
        archive = self.retain()
        failure = OSError("candidate sync failed")
        with (
            mock.patch.object(rebuild, "sync_fd", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            self.publish()
        self.assertIs(caught.exception, failure)
        self.assert_source()
        self.assertTrue(self.new.exists())
        self.assertTrue(archive.exists())

    def test_foreign_parent_and_same_file_refuse_without_mutation(
        self,
    ) -> None:
        for candidate in (self.db, self.home, self.home.parent / "outside"):
            with self.subTest(candidate=candidate), self.assertRaises(OSError):
                rebuild.publish_replacement(self.home, self.db, candidate)
        self.assert_source()

    @unittest.skipIf(sys.platform == "win32", "POSIX mode and link fixtures")
    def test_unsafe_modes_and_links_are_not_followed_or_repaired(self) -> None:
        self.db.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.retain()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o644)
        self.db.chmod(0o600)
        alias = self.home / "alias"
        alias.symlink_to(self.db)
        with self.assertRaises(OSError):
            rebuild.retain_store(self.home, alias)
        with self.assertRaises(OSError):
            rebuild.publish_replacement(self.home, self.db, alias)
        linked_home = self.home / "linked-home"
        linked_home.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError):
            rebuild.retain_store(linked_home, linked_home / self.db.name)
        self.assertEqual(erase_residue.aside_files(self.home), [])
        self.assert_source()

    @unittest.skipIf(sys.platform == "win32", "POSIX parent descriptor")
    def test_parent_sync_error_survives_secondary_close_failure(self) -> None:
        failure = OSError("first sync failure")
        real_close = os.close
        real_sync = rebuild.sync_fd

        def sync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise failure
            real_sync(fd)

        def close(fd: int) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            real_close(fd)
            if directory:
                raise OSError("secondary close failure")

        with (
            mock.patch.object(rebuild, "sync_fd", sync),
            mock.patch.object(os, "close", close),
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assert_source()
        self.assertEqual(len(erase_residue.aside_files(self.home)), 1)

    def test_changed_source_refuses_archive_success(self) -> None:
        replacement = self.write("other", b"different content")
        real_copy = rebuild.shutil.copyfileobj

        def copy(source: BinaryIO, output: BinaryIO) -> None:
            real_copy(source, output)
            # Windows replacement requires the completed reader to close.
            source.close()
            replacement.replace(self.db)

        with (
            mock.patch.object(rebuild.shutil, "copyfileobj", copy),
            self.assertRaisesRegex(OSError, "identity"),
        ):
            self.retain()
        self.assertEqual(self.db.read_bytes(), b"different content")
        self.assertEqual(len(erase_residue.aside_files(self.home)), 1)

    @unittest.skipIf(sys.platform == "win32", "fake Windows branch on POSIX")
    def test_windows_native_move_is_once_with_no_retry_or_parent_fsync(
        self,
    ) -> None:
        calls: list[tuple[Path, Path, bool, bool]] = []
        failure = OSError("native replace then raise")

        def move(
            source: Path, target: Path, *, replace: bool, retry_move: bool
        ) -> None:
            calls.append((source, target, replace, retry_move))
            if not replace and target.exists():
                raise FileExistsError("archive collision")
            source.replace(target)
            if replace:
                raise failure

        with (
            mock.patch.object(
                rebuild, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(
                rebuild.platform_windows,
                "assert_private",
                create=True,
            ),
            mock.patch.object(
                rebuild.platform_windows, "publish", move, create=True
            ),
            mock.patch.object(
                rebuild,
                "_sync_parent",
                side_effect=AssertionError("no Windows directory fsync"),
            ),
        ):
            archive = self.retain()
            with self.assertRaises(OSError) as caught:
                self.publish()
        self.assertIs(caught.exception, failure)
        self.assertEqual(
            calls,
            [
                (
                    archive.with_name(f"{archive.name}.partial"),
                    archive,
                    False,
                    False,
                ),
                (self.new, self.db, True, False),
            ],
        )
        self.assertEqual(self.db.read_bytes(), b"new replayed content")
        self.assertEqual(archive.read_bytes(), b"old governed content")
        self.assertEqual(erase_residue.aside_files(self.home), [str(archive)])

    @unittest.skipIf(sys.platform == "win32", "fake Windows branch on POSIX")
    def test_windows_archive_native_failure_keeps_enumerable_partial(
        self,
    ) -> None:
        failure = OSError("native archive failure")
        with (
            mock.patch.object(
                rebuild, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(
                rebuild.platform_windows,
                "assert_private",
                create=True,
            ),
            mock.patch.object(
                rebuild.platform_windows,
                "publish",
                side_effect=failure,
                create=True,
            ) as move,
            self.assertRaises(OSError) as caught,
        ):
            self.retain()
        self.assertIs(caught.exception, failure)
        self.assertEqual(move.call_count, 1)
        paths = erase_residue.aside_files(self.home)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".partial"))
        self.assertEqual(Path(paths[0]).read_bytes(), b"old governed content")
        self.assert_source()

    @unittest.skipIf(sys.platform == "win32", "POSIX process crash cutpoints")
    def test_process_crash_preserves_copy_residue_and_uncertain_new_live(
        self,
    ) -> None:
        script = """
import os, sys
from pathlib import Path
from muninn import platform_rebuild as rebuild
home = Path(sys.argv[1])
db = home / "muninn.sqlite"
if sys.argv[2] == "copy":
    def copy(source, output):
        output.write(source.read(4))
        output.flush()
        os._exit(71)
    rebuild.shutil.copyfileobj = copy
    rebuild.retain_store(home, db)
else:
    rebuild.retain_store(home, db)
    rebuild._sync_parent = lambda path: os._exit(72)
    rebuild.publish_replacement(home, db, home / "candidate")
"""
        copied = subprocess.run(
            [sys.executable, "-c", script, str(self.home), "copy"],
            check=False,
            capture_output=True,
        )
        self.assertEqual(copied.returncode, 71, copied.stderr)
        self.assert_source()
        paths = erase_residue.aside_files(self.home)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), b"old ")
        published = subprocess.run(
            [sys.executable, "-c", script, str(self.home), "publish"],
            check=False,
            capture_output=True,
        )
        self.assertEqual(published.returncode, 72, published.stderr)
        self.assertEqual(self.db.read_bytes(), b"new replayed content")
        self.assertFalse(self.new.exists())
        self.assertEqual(len(erase_residue.aside_files(self.home)), 2)

    def test_copy_or_first_close_error_survives_later_cleanup_errors(
        self,
    ) -> None:
        failure = OSError("first failure")
        original_open, original_close = platform_io.open_regular, os.close
        original_copy = rebuild.shutil.copyfileobj

        def opened(*args: object, **kwargs: object) -> BinaryIO:
            source = original_open(self.db)
            wrapped = mock.Mock(wraps=source)

            def close() -> None:
                source.close()
                raise OSError("later source close")

            wrapped.close.side_effect = close
            return wrapped

        def close(fd: int) -> None:
            original_close(fd)
            raise failure

        for copying in (True, False):
            with (
                self.subTest(copying=copying),
                mock.patch.object(platform_io, "open_regular", opened),
                mock.patch.object(os, "close", close),
                mock.patch.object(
                    rebuild.shutil,
                    "copyfileobj",
                    side_effect=failure if copying else original_copy,
                ),
                mock.patch.object(
                    Path, "unlink", side_effect=OSError("unlink forbidden")
                ) as unlink,
                self.assertRaises(OSError) as caught,
            ):
                self.retain()
            self.assertIs(caught.exception, failure)
            unlink.assert_not_called()
            self.assert_source()
        self.assertEqual(len(erase_residue.aside_files(self.home)), 2)
