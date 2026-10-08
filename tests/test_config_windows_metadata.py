"""Native config comparison preserves logical security components exactly."""

from __future__ import annotations

import struct
import unittest

from install.config_windows import security_parts


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
