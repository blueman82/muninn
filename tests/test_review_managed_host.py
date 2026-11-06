"""Check the public Darwin UUID ABI without any native identity query."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
from types import SimpleNamespace
from typing import Any
from unittest import TestCase, mock
from uuid import UUID

from muninn.review_json import canonical_json, canonical_json_value

UUID_TEXT = "00112233-4455-4677-8899-aabbccddeeff"


class ManagedHostTests(TestCase):
    """Use fixed fake ABI results and a controlled monotonic clock."""

    def setUp(self) -> None:
        try:
            self.host = importlib.import_module("muninn.review_managed_host")
        except ModuleNotFoundError:
            self.fail("the bounded physical host reader is missing")
        self.enterContext(mock.patch("platform.system", return_value="Darwin"))
        self.enterContext(mock.patch("platform.machine", return_value="arm64"))
        self.clock = self.enterContext(
            mock.patch.object(self.host.time, "monotonic", return_value=10.0)
        )
        self.native = mock.Mock(side_effect=self.call)
        self.loader = self.enterContext(
            mock.patch.object(
                self.host.ctypes,
                "CDLL",
                return_value=SimpleNamespace(gethostuuid=self.native),
            )
        )
        self.return_code = 0
        self.return_errno = 0
        self.raw = UUID(UUID_TEXT).bytes
        self.waits: list[tuple[int, int]] = []
        self.addCleanup(ctypes.set_errno, 0)

    def call(self, output: Any, wait: Any) -> int:
        """Write fixed fake bytes into caller-owned ABI buffers."""
        self.assertEqual(len(output), 16)
        self.assertEqual(bytes(output), b"\0" * 16)
        self.assertEqual(ctypes.get_errno(), 0)
        value = ctypes.cast(wait, ctypes.POINTER(self.host._Timespec)).contents
        self.waits.append((value.tv_sec, value.tv_nsec))
        for index, byte in enumerate(self.raw):
            output[index] = byte
        ctypes.set_errno(self.return_errno)
        return self.return_code

    def test_fixed_public_abi_returns_only_domain_separated_digest(
        self,
    ) -> None:
        ctypes.set_errno(errno.EPERM)
        value = self.host.observe(deadline=11.0)
        self.loader.assert_called_once_with(
            "/usr/lib/libSystem.B.dylib", use_errno=True
        )
        self.assertEqual(self.native.call_count, 1)
        self.assertEqual(self.waits, [(0, 50_000_000)])
        self.assertEqual(
            self.native.argtypes,
            (
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(self.host._Timespec),
            ),
        )
        self.assertIs(self.native.restype, ctypes.c_int)
        self.assertEqual(value.kind, "darwin-ioplatformuuid-v1")
        self.assertEqual(
            value.digest,
            hashlib.sha256(
                canonical_json_value(
                    [
                        "muninn-codex-managed-host-v1",
                        "Darwin",
                        "arm64",
                        UUID_TEXT,
                    ]
                ).encode()
            ).hexdigest(),
        )
        self.assertNotIn(UUID_TEXT, canonical_json(value))

    def test_shorter_deadline_stays_positive_and_below_native_cap(
        self,
    ) -> None:
        self.host.observe(deadline=10.001)
        seconds, nanoseconds = self.waits[0]
        self.assertEqual(seconds, 0)
        self.assertGreater(nanoseconds, 0)
        self.assertLessEqual(nanoseconds, 1_000_000)

    def test_large_finite_budget_keeps_the_native_wait_cap(self) -> None:
        self.host.observe(deadline=1e308)
        self.assertEqual(self.waits, [(0, 50_000_000)])

    def test_expired_or_subnanosecond_budget_never_calls_native(self) -> None:
        for deadline in (10.0, 9.0, 10.0000000001, float("nan"), float("inf")):
            with (
                self.subTest(deadline=deadline),
                self.assertRaisesRegex(
                    ValueError, "codex_host_identity_unavailable"
                ),
            ):
                self.host.observe(deadline=deadline)
        self.native.assert_not_called()

    def test_return_errno_and_invalid_uuid_refuse_without_retry(self) -> None:
        for status, error, raw in (
            (-1, errno.EPERM, UUID(UUID_TEXT).bytes),
            (0, errno.EPERM, UUID(UUID_TEXT).bytes),
            (0, 0, b"\0" * 16),
            (0, 0, b"\1" * 16),
        ):
            with self.subTest(status=status, error=error, raw=raw):
                self.return_code, self.return_errno, self.raw = (
                    status,
                    error,
                    raw,
                )
                self.native.reset_mock()
                with self.assertRaisesRegex(
                    ValueError, "codex_host_identity_unavailable"
                ):
                    self.host.observe(deadline=11.0)
                self.assertEqual(self.native.call_count, 1)

    def test_absent_api_and_elapsed_call_refuse_without_fallback(self) -> None:
        self.loader.side_effect = OSError("synthetic missing library")
        with self.assertRaisesRegex(
            ValueError, "codex_host_identity_unavailable"
        ):
            self.host.observe(deadline=11.0)
        self.native.assert_not_called()
        self.loader.side_effect = None
        self.loader.return_value = SimpleNamespace()
        with self.assertRaisesRegex(
            ValueError, "codex_host_identity_unavailable"
        ):
            self.host.observe(deadline=11.0)
        self.native.assert_not_called()
        self.loader.return_value = SimpleNamespace(gethostuuid=self.native)
        self.clock.side_effect = [10.0, 10.0, 11.1]
        with self.assertRaisesRegex(
            ValueError, "codex_host_identity_unavailable"
        ):
            self.host.observe(deadline=11.0)
        self.assertEqual(self.native.call_count, 1)
