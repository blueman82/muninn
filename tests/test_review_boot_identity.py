"""Validate candidate boot observations without rebooting a real host."""

from __future__ import annotations

import ctypes
import io
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from muninn import review_boot_identity as boot

UUID = "12345678-1234-4234-8234-123456789abc"


class BootIdentityTests(unittest.TestCase):
    """Only supported canonical native observations may authorize repair."""

    def test_linux_reads_one_bounded_native_boot_id(self) -> None:
        with (
            mock.patch.object(boot.sys, "platform", "linux"),
            mock.patch.object(
                boot.Path,
                "open",
                return_value=io.BytesIO((UUID + "\n").encode()),
            ),
        ):
            self.assertEqual(boot.observe(), "linux:" + UUID)

    def test_darwin_normalizes_the_exact_native_uuid(self) -> None:
        with (
            mock.patch.object(boot.sys, "platform", "darwin"),
            mock.patch.object(
                boot, "_darwin", return_value=UUID.upper().encode()
            ),
        ):
            self.assertEqual(boot.observe(), "darwin:" + UUID)

    def test_windows_never_guesses_an_observation(self) -> None:
        with (
            mock.patch.object(boot.sys, "platform", "win32"),
            self.assertRaisesRegex(ValueError, "boot_identity_unavailable"),
        ):
            boot.observe()

    def test_darwin_native_adapter_checks_exact_buffer_and_result(
        self,
    ) -> None:
        for result, size, raw in (
            (0, 37, UUID.encode() + b"\0"),
            (-1, 37, UUID.encode() + b"\0"),
            (0, 64, UUID.encode() + b"\0"),
            (0, 37, UUID.encode() + b"x"),
        ):

            def query(
                name: bytes,
                buffer: ctypes.Array[ctypes.c_char],
                length: Any,
                new: object,
                new_length: int,
                *,
                answer: tuple[int, int, bytes] = (result, size, raw),
            ) -> int:
                result, size, raw = answer
                self.assertEqual(name, b"kern.bootsessionuuid")
                self.assertEqual((new, new_length), (None, 0))
                pointer = ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))
                self.assertEqual(pointer.contents.value, 64)
                ctypes.memmove(buffer, raw, len(raw))
                pointer.contents.value = size
                return result

            native = mock.Mock(side_effect=query)
            with (
                self.subTest(result=result, size=size, terminated=raw[-1]),
                mock.patch.object(boot.sys, "platform", "darwin"),
                mock.patch.object(
                    boot.ctypes,
                    "CDLL",
                    return_value=SimpleNamespace(sysctlbyname=native),
                ) as library,
            ):
                if result == 0 and size == 37 and raw[-1] == 0:
                    self.assertEqual(boot.observe(), "darwin:" + UUID)
                else:
                    with self.assertRaisesRegex(
                        ValueError, "boot_identity_unavailable"
                    ):
                        boot.observe()
                library.assert_called_once_with(None, use_errno=True)
                self.assertIs(native.restype, ctypes.c_int)
                self.assertEqual(len(native.argtypes), 5)

    def test_malformed_or_unavailable_observations_refuse(self) -> None:
        for raw in (
            b"",
            b"x" * 1024,
            b"{12345678-1234-4234-8234-123456789abc}",
            b"0" * 36,
        ):
            with (
                self.subTest(raw=raw[:12]),
                mock.patch.object(boot.sys, "platform", "darwin"),
                mock.patch.object(boot, "_darwin", return_value=raw),
                self.assertRaisesRegex(
                    ValueError, "boot_identity_unavailable"
                ),
            ):
                boot.observe()
        with (
            mock.patch.object(boot.sys, "platform", "darwin"),
            mock.patch.object(
                boot, "_darwin", side_effect=OSError("native unavailable")
            ),
            self.assertRaisesRegex(ValueError, "boot_identity_unavailable"),
        ):
            boot.observe()
