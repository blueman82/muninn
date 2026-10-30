"""Shared safe opens preserve binary bytes and reject substituted sources."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

from muninn import platform_io


class SafeOpenTests(unittest.TestCase):
    """Read exactly the originally discovered regular file."""

    def test_binary_bytes_are_not_translated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source"
            data = b"one\r\ntwo\x1a\n"
            path.write_bytes(data)
            with platform_io.open_regular(path, root=Path(temp)) as handle:
                self.assertEqual(handle.read(), data)

    def test_outside_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir()
            path = Path(temp) / "outside"
            path.write_bytes(b"outside")
            with self.assertRaises(OSError):
                platform_io.open_regular(path, root=root)

    def test_directory_is_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(OSError),
        ):
            platform_io.open_regular(Path(temp))

    def test_replaced_file_identity_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, swap = Path(temp) / "source", Path(temp) / "swap"
            path.write_bytes(b"original")
            before = path.stat()
            swap.write_bytes(b"replacement")
            swap.replace(path)
            with self.assertRaises(OSError):
                platform_io.open_regular(path, expected=before)

    def test_private_file_write_uses_binary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "private"
            platform_io.ensure_private_dir(home)
            fd = platform_io.open_private(
                home / "file", os.O_CREAT | os.O_RDWR
            )
            try:
                os.write(fd, b"raw\n")
            finally:
                os.close(fd)
            self.assertEqual((home / "file").read_bytes(), b"raw\n")


@unittest.skipIf(os.name == "nt", "POSIX modes; Windows uses native ACLs")
class PrivateOpenTests(unittest.TestCase):
    """Keep private reopens stable and preserve unsafe-open failures."""

    def test_owned_private_reopen_preserves_metadata_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private"
            raw = b"retained\x00\r\n"
            path.write_bytes(raw)
            path.chmod(0o600)
            before = path.stat()
            descriptor = platform_io.open_private(path, os.O_RDWR)
            try:
                after = os.fstat(descriptor)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                ):
                    self.assertEqual(
                        getattr(after, field), getattr(before, field), field
                    )
                self.assertEqual(os.read(descriptor, len(raw) + 1), raw)
            finally:
                os.close(descriptor)
            self.assertEqual(path.read_bytes(), raw)

    def test_existing_modes_are_normalized_without_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private"
            for mode in (0o700, 0o644, 0o400, 0o1600):
                with self.subTest(mode=oct(mode)):
                    path.write_bytes(b"retain")
                    path.chmod(mode)
                    descriptor = platform_io.open_private(path, os.O_RDONLY)
                    try:
                        self.assertEqual(
                            stat.S_IMODE(os.fstat(descriptor).st_mode), 0o600
                        )
                        self.assertEqual(os.read(descriptor, 7), b"retain")
                    finally:
                        os.close(descriptor)

    def test_new_file_overrides_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous = os.umask(0o777)
            try:
                descriptor = platform_io.open_private(
                    Path(temp) / "private", os.O_CREAT | os.O_EXCL | os.O_RDWR
                )
            finally:
                os.umask(previous)
            try:
                self.assertEqual(
                    stat.S_IMODE(os.fstat(descriptor).st_mode), 0o600
                )
            finally:
                os.close(descriptor)

    def test_foreign_effective_owner_keeps_permission_failure(self) -> None:
        self.refuse_and_close("owner")

    def test_bsd_flags_keep_permission_failure(self) -> None:
        for flags in (2, 1, None, "unknown", False):
            with self.subTest(flags=flags):
                self.refuse_and_close("flags", flags)

    def test_fstat_failure_closes_descriptor_and_preserves_error(self) -> None:
        self.refuse_and_close("fstat")

    def test_fchmod_failure_closes_descriptor_and_preserves_error(
        self,
    ) -> None:
        self.refuse_and_close("mode")

    def refuse_and_close(self, condition: str, flags: object = 0) -> None:
        """Inject unavailable descriptor facts or chmod denial safely."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private"
            path.write_bytes(b"retain")
            path.chmod(0o644 if condition == "mode" else 0o600)
            descriptor = os.open(path, os.O_RDONLY)
            found = os.fstat(descriptor)
            facts = mock.Mock(
                spec=os.stat_result,
                **{
                    name: getattr(found, name)
                    for name in dir(found)
                    if name.startswith("st_")
                },
            )
            facts.st_flags = flags
            error = PermissionError("private descriptor permission denied")
            try:
                with (
                    mock.patch.object(os, "open", return_value=descriptor),
                    mock.patch.object(
                        os,
                        "fstat",
                        side_effect=error if condition == "fstat" else None,
                        return_value=facts,
                    ),
                    mock.patch.object(
                        os,
                        "geteuid",
                        return_value=found.st_uid + (condition == "owner"),
                    ),
                    mock.patch.object(os, "fchmod", side_effect=error),
                    self.assertRaises(PermissionError) as caught,
                ):
                    platform_io.open_private(path, os.O_RDONLY)
                self.assertIs(caught.exception, error)
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
                descriptor = -1
            finally:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
            self.assertEqual(path.read_bytes(), b"retain")

    def test_symlink_directory_and_fifo_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            target = home / "target"
            target.write_bytes(b"retained")
            target.chmod(0o600)
            link, fifo = home / "link", home / "fifo"
            link.symlink_to(target)
            if sys.platform != "win32":
                os.mkfifo(fifo, 0o600)
            for path in (link, home, fifo):
                with self.subTest(path=path), self.assertRaises(OSError):
                    platform_io.open_private(path, os.O_RDONLY)
            self.assertEqual(target.read_bytes(), b"retained")

    def test_truncation_is_refused_without_changing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private"
            path.write_bytes(b"retained")
            with self.assertRaises(ValueError):
                platform_io.open_private(path, os.O_WRONLY | os.O_TRUNC)
            self.assertEqual(path.read_bytes(), b"retained")
