"""Native config comparison preserves logical security components exactly."""

from __future__ import annotations

import ctypes
import struct
import unittest
from unittest import mock

from install import config_windows
from install.config_windows import SecurityMismatchError, security_parts


def descriptor(
    *, flags: int = 0x8404, ace_flags: int = 0x10, reverse: bool = False
) -> bytes:
    """Build synthetic self-relative metadata with a single private ACE."""
    owner = b"\x01\x01\x00\x00\x00\x00\x00\x05" + struct.pack("<I", 18)
    group = b"\x01\x01\x00\x00\x00\x00\x00\x05" + struct.pack("<I", 19)
    ace = struct.pack("<BBHI", 0, ace_flags, 20, 0x1F01FF) + owner
    acl = struct.pack("<BBHHH", 2, 0, 8 + len(ace), 1, 0) + ace
    parts = (group, owner, acl) if reverse else (owner, group, acl)
    owner_offset, group_offset = (32, 20) if reverse else (20, 32)
    return struct.pack(
        "<BBHLLLL", 1, 0, flags, owner_offset, group_offset, 0, 44
    ) + b"".join(parts)


def descriptor_difference(original: bytes, current: bytes) -> dict[str, int]:
    """Compare native components and layout without exposing descriptor bytes.

    Args:
        original: Synthetic config descriptor before replacement.
        current: The same synthetic path's descriptor after publication.

    Returns:
        Fixed numeric controls, equality, component offsets and lengths.

    Raises:
        OSError: If either descriptor fails the native component contract.
    """
    before, after = security_parts(original), security_parts(current)
    values = {
        "owner_equal": int(before[1] == after[1]),
        "group_equal": int(before[2] == after[2]),
        "dacl_equal": int(before[3] == after[3]),
    }
    for label, raw, parts in (
        ("original", original, before),
        ("final", current, after),
    ):
        values[label + "_control"] = parts[0]
        values[label + "_size"] = len(raw)
        for component, offset, value in zip(
            ("owner", "group", "dacl"), (4, 8, 16), parts[1:], strict=True
        ):
            values[f"{label}_{component}_offset"] = int.from_bytes(
                raw[offset : offset + 4], "little"
            )
            values[f"{label}_{component}_length"] = len(value)
    return values


class SecurityPartsTest(unittest.TestCase):
    """Offsets can vary; owner, group, control and ordered ACL bytes cannot."""

    def test_layout_offsets_do_not_change_components(self) -> None:
        self.assertEqual(
            security_parts(descriptor()),
            security_parts(descriptor(reverse=True)),
        )

    def test_inherited_ace_and_control_flags_remain_significant(self) -> None:
        original = security_parts(descriptor())
        self.assertNotEqual(original, security_parts(descriptor(ace_flags=0)))
        self.assertNotEqual(original, security_parts(descriptor(flags=0x9404)))

    def test_truncated_or_missing_dacl_refuses(self) -> None:
        raw = bytearray(descriptor())
        raw[16:20] = b"\x00" * 4
        for value in (bytes(raw), descriptor()[:-1], b"\x00" * 20):
            with self.assertRaises(OSError):
                security_parts(value)

    def test_mismatch_holds_only_fixed_control_and_equality_codes(
        self,
    ) -> None:
        before = security_parts(descriptor())
        after = security_parts(descriptor(flags=0x8004))
        error = SecurityMismatchError("control", before, after)
        self.assertIsInstance(error, PermissionError)
        self.assertEqual(error.control_before, 0x8404)
        self.assertEqual(error.control_after, 0x8004)
        self.assertEqual(
            (error.owner_equal, error.group_equal, error.dacl_equal), (1, 1, 1)
        )
        self.assertEqual(str(error), "config security differs: control")

    def test_exact_initial_security_does_not_call_setter(self) -> None:
        original = descriptor(flags=0x8004)
        with (
            mock.patch("install.config_windows.sys.platform", "win32"),
            mock.patch.object(
                config_windows, "_get", return_value=0, create=True
            ),
            mock.patch.object(config_windows, "_free", create=True),
            mock.patch.object(
                config_windows, "_set", return_value=0, create=True
            ) as setter,
            mock.patch.object(
                config_windows, "_descriptor_bytes", return_value=original
            ),
        ):
            config_windows._restore(
                5,
                ctypes.c_void_p(1),
                ctypes.c_void_p(2),
                ctypes.c_void_p(3),
                ctypes.c_void_p(4),
            )
            setter.assert_not_called()

    def test_changed_initial_uses_setter_and_exact_final_refusal(self) -> None:
        original = descriptor(flags=0x8004)
        changed = descriptor(flags=0x8404)
        with (
            mock.patch("install.config_windows.sys.platform", "win32"),
            mock.patch.object(
                config_windows, "_get", return_value=0, create=True
            ),
            mock.patch.object(config_windows, "_free", create=True),
            mock.patch.object(
                config_windows, "_set", return_value=0, create=True
            ) as setter,
            mock.patch.object(
                config_windows,
                "_descriptor_bytes",
                side_effect=[original, changed, changed],
            ),
        ):
            with self.assertRaises(SecurityMismatchError):
                config_windows._restore(
                    5,
                    ctypes.c_void_p(1),
                    ctypes.c_void_p(2),
                    ctypes.c_void_p(3),
                    ctypes.c_void_p(4),
                )
            setter.assert_called_once()

    def test_descriptor_difference_reports_numeric_components_and_layout(
        self,
    ) -> None:
        before = descriptor()
        after = descriptor(reverse=True)
        values = descriptor_difference(before, after)
        self.assertEqual(values["owner_equal"], 1)
        self.assertEqual(values["dacl_equal"], 1)
        self.assertEqual(values["original_owner_offset"], 20)
        self.assertEqual(values["final_owner_offset"], 32)
        self.assertTrue(
            all(isinstance(value, int) for value in values.values())
        )
        self.assertEqual(
            descriptor_difference(before, descriptor(ace_flags=0))[
                "dacl_equal"
            ],
            0,
        )
        with self.assertRaises(OSError):
            descriptor_difference(before, after[:-1])
