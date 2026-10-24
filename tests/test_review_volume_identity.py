"""Check native macOS volume identity observations without changing disks."""

from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from uuid import UUID

from muninn import review_volume_identity as volume

UUID_TEXT = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"


@unittest.skipIf(
    os.name == "nt", "macOS directory descriptors are unavailable"
)
class VolumeIdentityTests(unittest.TestCase):
    """Refuse missing or drifting native volume identifiers."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_native_observation_returns_canonical_uuid(self) -> None:
        """Read exactly one native UUID from the opened directory."""

        def query(
            fd: int,
            attrs: Any,
            buffer: ctypes.Array[ctypes.c_char],
            size: int,
            options: int,
        ) -> int:
            self.assertGreaterEqual(fd, 0)
            self.assertEqual(size, 20)
            self.assertEqual(options, 0)
            ctypes.memmove(
                buffer, (20).to_bytes(4, "little") + UUID(UUID_TEXT).bytes, 20
            )
            return 0

        native = mock.Mock(side_effect=query)
        with (
            mock.patch.object(volume.sys, "platform", "darwin"),
            mock.patch.object(
                volume.ctypes,
                "CDLL",
                return_value=SimpleNamespace(fgetattrlist=native),
            ),
        ):
            self.assertEqual(volume.observe(self.home), UUID_TEXT)
        self.assertIs(native.restype, ctypes.c_int)
        self.assertEqual(len(native.argtypes), 5)

    def test_bad_native_result_refuses(self) -> None:
        """A call error, short response, or nil UUID gives no authority."""
        for result, length, identifier in (
            (-1, 20, UUID(UUID_TEXT)),
            (0, 0, UUID(UUID_TEXT)),
            (0, 20, UUID(int=0)),
        ):

            def query(
                fd: int,
                attrs: Any,
                buffer: ctypes.Array[ctypes.c_char],
                size: int,
                options: int,
                *,
                answer: tuple[int, int, UUID] = (result, length, identifier),
            ) -> int:
                response, size_value, value = answer
                ctypes.memmove(
                    buffer, size_value.to_bytes(4, "little") + value.bytes, 20
                )
                return response

            native = mock.Mock(side_effect=query)
            with (
                self.subTest(result=result, length=length),
                mock.patch.object(volume.sys, "platform", "darwin"),
                mock.patch.object(
                    volume.ctypes,
                    "CDLL",
                    return_value=SimpleNamespace(fgetattrlist=native),
                ),
                self.assertRaisesRegex(ValueError, "quiescence_unverified"),
            ):
                volume.observe(self.home)

    def test_changed_directory_identity_refuses(self) -> None:
        """A path replacement cannot borrow a UUID from the held folder."""
        moved = self.home.with_name(self.home.name + "-moved")

        def restore() -> None:
            self.home.rmdir()
            moved.rename(self.home)

        def query(
            fd: int,
            attrs: Any,
            buffer: ctypes.Array[ctypes.c_char],
            size: int,
            options: int,
        ) -> int:
            self.home.rename(moved)
            self.home.mkdir(mode=0o700)
            self.addCleanup(restore)
            ctypes.memmove(
                buffer, (20).to_bytes(4, "little") + UUID(UUID_TEXT).bytes, 20
            )
            return 0

        with (
            mock.patch.object(volume.sys, "platform", "darwin"),
            mock.patch.object(
                volume.ctypes,
                "CDLL",
                return_value=SimpleNamespace(
                    fgetattrlist=mock.Mock(side_effect=query)
                ),
            ),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            volume.observe(self.home)

    def test_non_darwin_refuses(self) -> None:
        """This adapter does not invent volume proof on other systems."""
        with (
            mock.patch.object(volume.sys, "platform", "linux"),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            volume.observe(self.home)
