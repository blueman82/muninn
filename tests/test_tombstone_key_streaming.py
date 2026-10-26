"""Bound raw key-presence scans without weakening key-creation guards."""

from __future__ import annotations

import gc
import io
import os
import sys
import tempfile
import tracemalloc
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from muninn import erase_collect, platform_io, store, tombstone_key

CHUNK = 64 * 1024
MEMORY_LIMIT = 1024 * 1024


class FaultedReader(io.BufferedReader):
    """Expose the same failure to whole reads and later bounded reads."""

    def __init__(self, path: Path, failure: BaseException) -> None:
        """Open a real private descriptor with an injected read failure."""
        super().__init__(io.FileIO(path, "r"))
        self.failure = failure
        self.failed = False

    def read(self, size: int | None = -1, /) -> bytes:
        """Refuse a whole read or the read following the first chunk."""
        if size is None or size < 0 or self.tell() >= CHUNK:
            self.failed = True
            raise self.failure
        return super().read(size)


class PartitionedReader(io.BufferedReader):
    """Partition bounded reads while retaining whole-read semantics."""

    def __init__(self, path: Path, parts: tuple[int, ...]) -> None:
        """Open a private fixture with arbitrary positive short reads."""
        super().__init__(io.FileIO(path, "r"))
        self.parts = iter(parts)

    def read(self, size: int | None = -1, /) -> bytes:
        """Honor whole reads and partition only a bounded request."""
        if size is None or size < 0:
            return super().read(size)
        return super().read(min(size, next(self.parts, size)))


class KeyScanTests(unittest.TestCase):
    """Use only private temporary journals, keys and marked stores."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        store.ensure_private_dir(self.home)
        self.path = self.home / tombstone_key.TOMBSTONE_FILE
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)

    def write(self, data: bytes) -> None:
        """Publish one synthetic private journal without touching live data."""
        self.path.unlink(missing_ok=True)
        fd = platform_io.open_private(
            self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def large_journal(self) -> None:
        """Create more than ten MiB of valid duplicate unkeyed intent."""
        row = (
            b'{"provider":"codex","level":"session",'
            b'"session_root":"synthetic-session","created_at":1}\n'
        )
        self.write(row * (10 * MEMORY_LIMIT // len(row) + 1))

    def peak(self, operation: Callable[[], object]) -> tuple[object, int]:
        """Measure the actual call after fixture allocation has completed."""
        gc.collect()
        tracemalloc.start()
        try:
            result = operation()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        return result, peak

    def test_large_unkeyed_journal_does_not_allocate_its_full_bytes(
        self,
    ) -> None:
        self.large_journal()
        result, peak = self.peak(
            lambda: tombstone_key.log_has_keyed(self.home)
        )
        self.assertFalse(result)
        self.assertLess(peak, MEMORY_LIMIT)

    def test_present_key_without_table_tags_has_bounded_scan_memory(
        self,
    ) -> None:
        self.large_journal()
        expected = tombstone_key.load_key(self.home)
        path = self.home / tombstone_key.KEY_FILE
        identity = path.stat().st_ino
        self.assertFalse(tombstone_key.table_has_keyed(self.conn))
        result, peak = self.peak(lambda: tombstone_key.key_for(self.conn))
        self.assertEqual(result, expected)
        self.assertEqual(path.stat().st_ino, identity)
        self.assertEqual(path.read_bytes(), expected)
        self.assertLess(peak, MEMORY_LIMIT)

    def test_content_erasure_consumer_has_bounded_prescan_memory(
        self,
    ) -> None:
        self.large_journal()
        expected = tombstone_key.load_key(self.home)
        target = erase_collect.Target()
        target.content.append(
            ("codex", "synthetic-thread", "user", "synthetic content")
        )
        result, peak = self.peak(
            lambda: erase_collect.content_tombstones(self.conn, target)
        )
        self.assertIsInstance(result, list)
        assert isinstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(tombstone_key.load_key(self.home), expected)
        self.assertLess(peak, MEMORY_LIMIT)

    def test_every_marker_boundary_split_and_late_marker_are_seen(
        self,
    ) -> None:
        for offset in (*range(CHUNK - 4, CHUNK + 1), 3 * CHUNK + 17):
            with self.subTest(offset=offset):
                self.write(b"x" * offset + b"ev2:" + b"y" * CHUNK)
                self.assertTrue(tombstone_key.log_has_keyed(self.home))

    def test_all_repeated_short_read_partitions_retain_the_marker(
        self,
    ) -> None:
        for cuts in range(8):
            parts: list[int] = []
            width = 1
            for offset in range(3):
                if cuts & (1 << offset):
                    parts.append(width)
                    width = 1
                else:
                    width += 1
            parts.append(width)
            with self.subTest(parts=parts):
                self.write(b"xxev2:tail")
                reader = PartitionedReader(self.path, (2, *parts))
                with mock.patch.object(
                    platform_io, "open_regular", return_value=reader
                ):
                    self.assertTrue(tombstone_key.log_has_keyed(self.home))
                self.assertTrue(reader.closed)

    def test_empty_partial_and_raw_non_json_marker_outcomes_are_kept(
        self,
    ) -> None:
        for raw, expected in (
            (b"", False),
            (b"x" * CHUNK + b"e", False),
            (b"x" * CHUNK + b"ev", False),
            (b"x" * CHUNK + b"ev2", False),
            (b"x" * CHUNK + b"ev2:", True),
            (b"\xffnot-json ev2:\x80", True),
            (b'{"other":"ev2:"}', True),
        ):
            with self.subTest(size=len(raw), expected=expected):
                self.write(raw)
                self.assertEqual(
                    tombstone_key.log_has_keyed(self.home), expected
                )

    def test_late_io_error_is_conservative_and_closes_the_descriptor(
        self,
    ) -> None:
        for marker in (b"", b"ev2:"):
            with self.subTest(marker_present=bool(marker)):
                self.write(marker + b"x" * (CHUNK - len(marker)) + b"end")
                reader = FaultedReader(self.path, OSError("synthetic"))
                with mock.patch.object(
                    platform_io, "open_regular", return_value=reader
                ):
                    self.assertTrue(tombstone_key.log_has_keyed(self.home))
                self.assertTrue(reader.failed)
                self.assertTrue(reader.closed)

    def test_cancellation_after_a_marker_still_propagates_and_closes(
        self,
    ) -> None:
        self.write(b"ev2:" + b"x" * CHUNK)
        reader = FaultedReader(self.path, KeyboardInterrupt())
        with (
            mock.patch.object(
                platform_io, "open_regular", return_value=reader
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            tombstone_key.log_has_keyed(self.home)
        self.assertTrue(reader.failed)
        self.assertTrue(reader.closed)

    def test_missing_file_is_false_and_other_discovery_error_is_true(
        self,
    ) -> None:
        self.assertFalse(tombstone_key.log_has_keyed(self.home))
        with mock.patch.object(Path, "lstat", side_effect=PermissionError):
            self.assertTrue(tombstone_key.log_has_keyed(self.home))

    def test_private_fd_refusal_is_conservative_and_closes(self) -> None:
        self.write(b"unkeyed")
        reader = platform_io.open_regular(self.path, root=self.home)
        with (
            mock.patch.object(
                platform_io, "open_regular", return_value=reader
            ),
            mock.patch.object(
                platform_io, "assert_private_fd", side_effect=PermissionError
            ),
        ):
            self.assertTrue(tombstone_key.log_has_keyed(self.home))
        self.assertTrue(reader.closed)

    def test_actual_substitution_after_discovery_is_conservative(
        self,
    ) -> None:
        self.write(b"original")
        original = platform_io.open_regular

        def replace(
            path: Path,
            *,
            root: Path | None = None,
            expected: os.stat_result | None = None,
        ) -> BinaryIO:
            path.rename(self.home / "original.jsonl")
            self.write(b"replacement")
            return original(path, root=root, expected=expected)

        with mock.patch.object(
            platform_io, "open_regular", side_effect=replace
        ):
            self.assertTrue(tombstone_key.log_has_keyed(self.home))
        self.assertEqual(self.path.read_bytes(), b"replacement")

    @unittest.skipIf(sys.platform == "win32", "POSIX nofollow contract")
    def test_actual_symlink_is_not_followed(self) -> None:
        target = self.home / "target"
        target.write_bytes(b"unkeyed")
        self.path.symlink_to(target)
        self.assertTrue(tombstone_key.log_has_keyed(self.home))
        self.assertEqual(target.read_bytes(), b"unkeyed")

    def test_journal_only_late_marker_refuses_a_missing_required_key(
        self,
    ) -> None:
        self.write(b"x" * (2 * CHUNK - 2) + b"ev2:" + b"a" * 64)
        with self.assertRaises(tombstone_key.TombstoneKeyError):
            tombstone_key.key_for(self.conn)
        self.assertFalse((self.home / tombstone_key.KEY_FILE).exists())

    def test_table_keyed_history_short_circuits_prescan_and_refuses(
        self,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tombstone(created_at,provider,level,thread_id,"
            "line,line_sha256) VALUES(1,'codex','line','synthetic',0,?)",
            ("ev2:" + "a" * 64,),
        )
        with (
            mock.patch.object(
                tombstone_key, "log_has_keyed", side_effect=AssertionError
            ),
            self.assertRaises(tombstone_key.TombstoneKeyError),
        ):
            tombstone_key.key_for(self.conn)
        self.assertFalse((self.home / tombstone_key.KEY_FILE).exists())

    def test_corrupt_present_key_is_not_replaced_after_unkeyed_scan(
        self,
    ) -> None:
        self.write(b"unkeyed")
        tombstone_key.load_key(self.home)
        path = self.home / tombstone_key.KEY_FILE
        path.write_bytes(b"damaged")
        identity = path.stat().st_ino
        with self.assertRaises(tombstone_key.TombstoneKeyError):
            tombstone_key.key_for(self.conn)
        self.assertEqual(path.read_bytes(), b"damaged")
        self.assertEqual(path.stat().st_ino, identity)

    def test_unkeyed_history_still_allows_a_first_whole_key(self) -> None:
        self.write(b"unkeyed")
        key = tombstone_key.key_for(self.conn)
        self.assertEqual(len(key), tombstone_key.KEY_BYTES)
        self.assertEqual(
            (self.home / tombstone_key.KEY_FILE).read_bytes(), key
        )
