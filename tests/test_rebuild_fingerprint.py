"""Detect intervening changes to private rebuild source files."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, cast
from unittest import mock

from muninn import platform_io, platform_windows, store
from muninn import store_fingerprint as fingerprint
from muninn.tombstone_key import KEY_FILE, TOMBSTONE_FILE
from tests.store_support import public_read


class FingerprintTests(unittest.TestCase):
    """Use ordinary files and actual SQLite commits in temporary homes."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.home = self.parent / "data"
        platform_io.ensure_private_dir(self.home)
        self.db = store.db_path(self.home)
        with closing(store.connect_rw(self.db)) as conn:
            conn.execute(
                "INSERT INTO scope(key,label,kind) "
                "VALUES('/repo','before','dir')"
            )
        self.key = self.home / KEY_FILE
        self.journal = self.home / TOMBSTONE_FILE
        self.write(self.key, b"k" * 32)
        self.write(self.journal, b'{"intent":"before"}\n')

    def write(self, path: Path, data: bytes) -> None:
        """Create a private synthetic file without inherited loose modes."""
        fd = platform_io.open_private(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def test_capture_and_verify_have_no_store_or_process_authority(
        self,
    ) -> None:
        before = {
            p.name: p.read_bytes() for p in (self.db, self.key, self.journal)
        }
        with (
            mock.patch.object(sqlite3, "connect", side_effect=AssertionError),
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError
            ),
            mock.patch("subprocess.Popen", side_effect=AssertionError),
        ):
            original = fingerprint.capture(self.home)
            fingerprint.verify(self.home, original)
        self.assertEqual(
            original.database.digest,
            hashlib.sha256(before[self.db.name]).hexdigest(),
        )
        self.assertEqual(
            original.database.identity,
            (self.db.stat().st_dev, self.db.stat().st_ino),
        )
        self.assertEqual(
            before,
            {
                p.name: p.read_bytes()
                for p in (self.db, self.key, self.journal)
            },
        )

    def test_windows_path_and_descriptor_ctime_use_their_own_baselines(
        self,
    ) -> None:
        """Keep unchanged files despite Windows path and fd ctime views."""
        original_lstat = Path.lstat
        original_fstat = os.fstat

        def view(actual: os.stat_result, ctime_ns: int) -> os.stat_result:
            return cast(
                os.stat_result,
                types.SimpleNamespace(
                    st_dev=actual.st_dev,
                    st_ino=actual.st_ino,
                    st_size=actual.st_size,
                    st_mode=actual.st_mode,
                    st_uid=actual.st_uid,
                    st_mtime_ns=actual.st_mtime_ns,
                    st_ctime_ns=ctime_ns,
                    st_birthtime_ns=30,
                ),
            )

        def path_stat(path: Path) -> os.stat_result:
            actual = original_lstat(path)
            if path != self.db:
                return actual
            return view(actual, 10)

        def descriptor_stat(fd: int) -> os.stat_result:
            return view(original_fstat(fd), 20)

        with (
            mock.patch.object(
                fingerprint, "sys", types.SimpleNamespace(platform="win32")
            ),
            mock.patch.object(platform_windows, "assert_private", create=True),
            mock.patch.object(Path, "lstat", path_stat),
            mock.patch.object(os, "fstat", descriptor_stat),
        ):
            result = fingerprint.capture_file(self.db)
        self.assertEqual(
            result.digest, hashlib.sha256(self.db.read_bytes()).hexdigest()
        )
        self.assertEqual(result.ctime_ns, 10)

    def test_real_commit_with_same_size_and_mtime_is_detected(self) -> None:
        original = fingerprint.capture(self.home)
        before = self.db.stat()
        with (
            store.writer_lock(self.home),
            closing(store.connect_rw(self.db)) as conn,
        ):
            conn.execute("UPDATE scope SET label='after!' WHERE key='/repo'")
        os.utime(self.db, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.db.stat()
        self.assertEqual(
            (before.st_size, before.st_mtime_ns),
            (after.st_size, after.st_mtime_ns),
        )
        self.assertNotEqual(
            original.database.digest,
            fingerprint.capture(self.home).database.digest,
        )
        with self.assertRaisesRegex(
            fingerprint.SourceChangedError, "^source_changed$"
        ):
            fingerprint.verify(self.home, original)

    def test_each_same_byte_inode_replacement_is_detected(self) -> None:
        for path in (self.db, self.key, self.journal):
            with self.subTest(path=path.name):
                original = fingerprint.capture(self.home)
                before = path.read_bytes()
                replacement = path.with_name("replacement")
                self.write(replacement, before)
                replacement.replace(path)
                self.assertEqual(path.read_bytes(), before)
                with self.assertRaises(fingerprint.SourceChangedError):
                    fingerprint.verify(self.home, original)

    def test_home_replacement_preserving_file_inodes_is_detected(self) -> None:
        original = fingerprint.capture(self.home)
        old = self.parent / "retained-home"
        self.home.rename(old)
        platform_io.ensure_private_dir(self.home)
        for path in old.iterdir():
            path.rename(self.home / path.name)
        self.assertEqual(
            original.database.identity,
            fingerprint.capture(self.home).database.identity,
        )
        with self.assertRaises(fingerprint.SourceChangedError):
            fingerprint.verify(self.home, original)

    def test_key_and_intent_presence_and_bytes_are_bound(self) -> None:
        for path in (self.key, self.journal):
            with self.subTest(path=path.name):
                original = fingerprint.capture(self.home)
                raw, before = path.read_bytes(), path.stat()
                path.write_bytes(bytes(value ^ 1 for value in raw))
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                with self.assertRaises(fingerprint.SourceChangedError):
                    fingerprint.verify(self.home, original)
                path.unlink()
                with self.assertRaises(fingerprint.SourceChangedError):
                    fingerprint.verify(self.home, original)
                absent = fingerprint.capture(self.home)
                self.write(path, raw)
                with self.assertRaises(fingerprint.SourceChangedError):
                    fingerprint.verify(self.home, absent)

    def test_each_sidecar_refuses_without_reading_any_content(self) -> None:
        original = fingerprint.capture(self.home)
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = Path(f"{self.db}{suffix}")
                self.write(sidecar, b"unknown transaction")
                with (
                    mock.patch.object(platform_io, "open_regular") as opening,
                    self.assertRaises(fingerprint.SourceChangedError),
                ):
                    fingerprint.verify(self.home, original)
                opening.assert_not_called()
                self.assertEqual(sidecar.read_bytes(), b"unknown transaction")
                sidecar.unlink()

    def test_private_file_digest_streams_every_byte_and_closes(self) -> None:
        archive = self.home / "muninn.sqlite.recovery-test"
        raw = b"x" * (3 * 1024 * 1024) + b"last bytes matter"
        self.write(archive, raw)
        actual_open = platform_io.open_regular
        lengths: list[int] = []
        handles: list[BinaryIO] = []

        def opening(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            handle = actual_open(path, root=root, expected=expected)
            handles.append(handle)
            proxy = mock.Mock(wraps=handle)

            def read(size: int) -> bytes:
                lengths.append(size)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 1024 * 1024)
                return handle.read(size)

            proxy.read.side_effect = read
            return proxy

        with mock.patch.object(platform_io, "open_regular", opening):
            captured = fingerprint.capture_file(archive)
        self.assertEqual(
            (captured.size, captured.digest),
            (len(raw), hashlib.sha256(raw).hexdigest()),
        )
        self.assertGreater(len(lengths), 3)
        self.assertTrue(all(handle.closed for handle in handles))

    def test_unsafe_privacy_is_refused_without_repair(self) -> None:
        for path in (self.home, self.db, self.key, self.journal):
            with (
                self.subTest(path=path.name),
                public_read(self, path, directory=path == self.home),
                self.assertRaises(fingerprint.SourceChangedError),
            ):
                fingerprint.capture(self.home)

    def test_links_and_nonfiles_refuse_without_reading_the_target(
        self,
    ) -> None:
        target = self.parent / "private-original"
        self.write(target, b"content must remain untouched")
        for path in (self.db, self.key, self.journal):
            raw = path.read_bytes()
            for kind in ("link", "directory"):
                with self.subTest(path=path.name, kind=kind):
                    path.unlink()
                    if kind == "link":
                        path.symlink_to(target)
                    else:
                        path.mkdir(mode=0o700)
                    with self.assertRaises(fingerprint.SourceChangedError):
                        fingerprint.capture(self.home)
                    self.assertEqual(
                        target.read_bytes(), b"content must remain untouched"
                    )
                    if kind == "link":
                        path.unlink()
                    else:
                        path.rmdir()
                    self.write(path, raw)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(f"{self.db}{suffix}")
            sidecar.symlink_to(self.home / "absent")
            with self.assertRaises(fingerprint.SourceChangedError):
                fingerprint.capture(self.home)
            sidecar.unlink()

    def test_open_race_binds_the_discovered_file_and_closes_old_handle(
        self,
    ) -> None:
        actual_open = platform_io.open_regular
        handles: list[BinaryIO] = []
        for after_open in (False, True):
            handles.clear()

            def replace(
                path: Path,
                *,
                root: Path | None = None,
                expected: os.stat_result | None = None,
                after_open: bool = after_open,
            ) -> BinaryIO:
                if after_open:
                    handles.append(
                        actual_open(path, root=root, expected=expected)
                    )
                replacement = path.with_name("replacement")
                self.write(replacement, path.read_bytes())
                replacement.replace(path)
                return (
                    handles[0]
                    if after_open
                    else actual_open(path, root=root, expected=expected)
                )

            with (
                self.subTest(after_open=after_open),
                mock.patch.object(platform_io, "open_regular", replace),
                self.assertRaises(fingerprint.SourceChangedError),
            ):
                fingerprint.capture_file(self.db)
            self.assertTrue(all(handle.closed for handle in handles))

    def test_read_error_precedes_close_error_and_successful_close_can_fail(
        self,
    ) -> None:
        actual_open = platform_io.open_regular
        original, secondary = OSError("private read"), OSError("close")
        handles: list[BinaryIO] = []
        for fail_read in (True, False):
            handles.clear()

            def opening(
                path: Path,
                *,
                root: Path | None = None,
                expected: os.stat_result | None = None,
                fail_read: bool = fail_read,
            ) -> BinaryIO:
                handle = actual_open(path, root=root, expected=expected)
                handles.append(handle)
                proxy = mock.Mock(wraps=handle)
                if fail_read:
                    proxy.read.side_effect = original

                def close() -> None:
                    handle.close()
                    raise secondary

                proxy.close.side_effect = close
                return proxy

            with (
                self.subTest(fail_read=fail_read),
                mock.patch.object(platform_io, "open_regular", opening),
                self.assertRaises(fingerprint.SourceChangedError) as caught,
            ):
                fingerprint.capture_file(self.db)
            self.assertIs(
                caught.exception.__cause__,
                original if fail_read else secondary,
            )
            self.assertEqual(str(caught.exception), "source_changed")
            self.assertTrue(all(handle.closed for handle in handles))

    def test_final_checks_refuse_changes_to_already_observed_state(
        self,
    ) -> None:
        actual_open = platform_io.open_regular
        saved = self.db.read_bytes(), self.key.read_bytes()
        for changed in ("absent_key", "database", "sidecar"):
            if changed == "absent_key":
                self.key.unlink()
            trigger = self.key if changed == "database" else self.journal

            def mutate(
                path: Path,
                *,
                root: Path | None = None,
                expected: os.stat_result | None = None,
                trigger: Path = trigger,
                changed: str = changed,
            ) -> BinaryIO:
                if path == trigger:
                    if changed == "absent_key":
                        self.write(self.key, saved[1])
                    elif changed == "database":
                        self.db.write_bytes(b"changed" + saved[0][7:])
                    else:
                        self.write(Path(f"{self.db}-journal"), b"pending")
                return actual_open(path, root=root, expected=expected)

            with (
                self.subTest(changed=changed),
                mock.patch.object(platform_io, "open_regular", mutate),
                self.assertRaises(fingerprint.SourceChangedError),
            ):
                fingerprint.capture(self.home)
            self.db.write_bytes(saved[0])
            Path(f"{self.db}-journal").unlink(missing_ok=True)

    def test_missing_database_and_unreadable_metadata_refuse(self) -> None:
        actual_stat = Path.lstat
        for denied in (self.db, self.key, Path(f"{self.db}-journal")):

            def metadata(path: Path, denied: Path = denied) -> os.stat_result:
                if path == denied:
                    raise PermissionError("private path")
                return actual_stat(path)

            with (
                self.subTest(denied=denied.name),
                mock.patch.object(Path, "lstat", metadata),
                self.assertRaises(fingerprint.SourceChangedError) as caught,
            ):
                fingerprint.capture(self.home)
            self.assertEqual(str(caught.exception), "source_changed")
        self.db.unlink()
        with self.assertRaises(fingerprint.SourceChangedError):
            fingerprint.capture(self.home)
        self.assertFalse(self.db.exists())
