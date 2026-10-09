"""Native config comparison preserves logical security components exactly."""

from __future__ import annotations

import ctypes
import struct
import unittest
from ctypes import wintypes
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


def _acl(raw: bytes) -> tuple[tuple[int, ...], list[bytes], bytes]:
    """Validate at most sixteen ordinary ACEs without exporting their SIDs."""
    if len(raw) < 8:
        raise OSError("truncated synthetic ACL")
    header = struct.unpack("<BBHHH", raw[:8])
    if header[2] != len(raw) or header[3] > 16:
        raise OSError("unsupported synthetic ACL size")
    entries: list[bytes] = []
    offset = 8
    for _ in range(header[3]):
        if offset + 16 > len(raw):
            raise OSError("truncated synthetic ACE")
        kind, _, size = struct.unpack("<BBH", raw[offset : offset + 4])
        if kind not in (0, 1) or size < 16 or offset + size > len(raw):
            raise OSError("unsupported synthetic ACE")
        entry = raw[offset : offset + size]
        sid_size = 8 + 4 * entry[9]
        if entry[8] != 1 or entry[9] > 15 or 8 + sid_size > size:
            raise OSError("invalid synthetic ACE SID")
        entries.append(entry)
        offset += size
    return header, entries, raw[offset:]


def acl_difference(original: bytes, current: bytes) -> dict[str, int]:
    """Compare ACL headers and ordered ACE fields with numeric codes.

    Args:
        original: Validated original synthetic descriptor's DACL component.
        current: Same synthetic config's DACL after publication.

    Returns:
        Header fields, at most sixteen ordered ACE fields and SID equality.

    Raises:
        OSError: If a component has unsupported or truncated ACEs.
    """
    before, before_entries, before_tail = _acl(original)
    after, after_entries, after_tail = _acl(current)
    values: dict[str, int] = {}
    for label, header, tail in (
        ("original", before, before_tail),
        ("final", after, after_tail),
    ):
        for field, value in zip(
            ("revision", "reserved1", "size", "count", "reserved2"),
            header,
            strict=True,
        ):
            values[f"{label}_acl_{field}"] = value
        values[f"{label}_acl_free_size"] = len(tail)
    values["acl_free_equal"] = int(before_tail == after_tail)
    for index, (first, second) in enumerate(
        zip(before_entries, after_entries, strict=False)
    ):
        for label, entry in (("original", first), ("final", second)):
            for field, value in zip(
                ("type", "flags", "size", "mask"),
                struct.unpack("<BBHI", entry[:8]),
                strict=True,
            ):
                values[f"ace_{index}_{label}_{field}"] = value
        first_end, second_end = 16 + 4 * first[9], 16 + 4 * second[9]
        values[f"ace_{index}_sid_equal"] = int(
            first[8:first_end] == second[8:second_end]
        )
        values[f"ace_{index}_padding_equal"] = int(
            first[first_end:] == second[second_end:]
        )
    return values


def stage_difference(
    original: tuple[int, bytes, bytes, bytes],
    current: tuple[int, bytes, bytes, bytes],
) -> dict[str, int]:
    """Compare bound security components without disclosing their contents.

    Args:
        original: Original synthetic path's validated descriptor components.
        current: Descriptor-bound or pathname-bound components at one stage.

    Returns:
        Only native controls and exact component equality codes.
    """
    return {
        "control_before": original[0],
        "control_after": current[0],
        "owner_equal": int(original[1] == current[1]),
        "group_equal": int(original[2] == current[2]),
        "dacl_equal": int(original[3] == current[3]),
    }


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
    values.update(acl_difference(before[3], after[3]))
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
                config_windows, "_read", return_value=security_parts(original)
            ),
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
                config_windows, "_read", return_value=security_parts(changed)
            ),
            mock.patch.object(
                config_windows, "_set", return_value=0, create=True
            ) as setter,
            mock.patch.object(
                config_windows,
                "_descriptor_bytes",
                return_value=original,
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

    def test_acl_difference_identifies_reserved_flags_mask_and_sid(
        self,
    ) -> None:
        original = security_parts(descriptor())[3]
        changed = bytearray(original)
        changed[1] = 3
        changed[9] = 0
        changed[12:16] = (0x10000000).to_bytes(4, "little")
        changed[-1] ^= 1
        values = acl_difference(original, bytes(changed))
        self.assertEqual(values["original_acl_reserved1"], 0)
        self.assertEqual(values["final_acl_reserved1"], 3)
        self.assertEqual(values["ace_0_original_flags"], 0x10)
        self.assertEqual(values["ace_0_final_flags"], 0)
        self.assertEqual(values["ace_0_final_mask"], 0x10000000)
        self.assertEqual(values["ace_0_sid_equal"], 0)
        self.assertTrue(
            all(isinstance(value, int) for value in values.values())
        )

    def test_acl_difference_bounds_entries_and_invalid_sizes(self) -> None:
        original = security_parts(descriptor())[3]
        too_many = bytearray(original)
        too_many[4:6] = (17).to_bytes(2, "little")
        invalid = bytearray(original)
        invalid[10:12] = (4).to_bytes(2, "little")
        for changed in (bytes(too_many), bytes(invalid), original[:-1]):
            with self.assertRaises(OSError):
                acl_difference(original, changed)

    def test_stage_comparison_exposes_only_numeric_security_equality(
        self,
    ) -> None:
        before = security_parts(descriptor())
        current = security_parts(descriptor(ace_flags=0))
        values = stage_difference(before, current)
        self.assertEqual(
            values,
            {
                "control_before": 0x8404,
                "control_after": 0x8404,
                "owner_equal": 1,
                "group_equal": 1,
                "dacl_equal": 0,
            },
        )
        self.assertTrue(
            all(isinstance(value, int) for value in values.values())
        )


class BoundRawDescriptorTest(unittest.TestCase):
    """Read raw security from the retained source handle before mutation."""

    def test_raw_read_uses_bound_handle_and_validates_returned_components(
        self,
    ) -> None:
        original = descriptor(flags=0x8004, ace_flags=0)
        calls: list[tuple[int, int, int]] = []

        def query(
            handle: int,
            information: int,
            buffer: ctypes.Array[ctypes.c_char] | None,
            length: int,
            needed: ctypes.c_void_p,
        ) -> int:
            calls.append((handle, information, length))
            ctypes.cast(
                needed, ctypes.POINTER(wintypes.DWORD)
            ).contents.value = len(original)
            if buffer is None:
                return 0
            ctypes.memmove(buffer, original, len(original))
            return 1

        with (
            mock.patch("install.config_windows.sys.platform", "win32"),
            mock.patch.object(
                config_windows, "_query", side_effect=query, create=True
            ),
            mock.patch.object(
                ctypes, "get_last_error", return_value=122, create=True
            ),
        ):
            self.assertEqual(config_windows._raw_descriptor(77), original)
            self.assertEqual(
                config_windows._read(77), security_parts(original)
            )
        self.assertEqual(calls, [(77, 7, 0), (77, 7, len(original))] * 2)

    def test_raw_query_refuses_outside_windows(self) -> None:
        with (
            mock.patch("install.config_windows.sys.platform", "darwin"),
            self.assertRaises(OSError),
        ):
            config_windows._raw_descriptor(77)

    def test_raw_query_refuses_native_errors_and_invalid_sizes(self) -> None:
        for first_size, first_error, second_size, success in (
            (72, 5, 72, 1),
            (19, 122, 19, 1),
            (1048577, 122, 1048577, 1),
            (72, 122, 73, 1),
            (72, 122, 19, 1),
            (72, 122, 72, 0),
        ):
            with self.subTest(
                first_size=first_size,
                first_error=first_error,
                second_size=second_size,
                success=success,
            ):

                def query(
                    handle: int,
                    information: int,
                    buffer: ctypes.Array[ctypes.c_char] | None,
                    length: int,
                    needed: ctypes.c_void_p,
                    case: tuple[int, int, int] = (
                        first_size,
                        second_size,
                        success,
                    ),
                ) -> int:
                    initial, returned, result = case
                    size = initial if buffer is None else returned
                    ctypes.cast(
                        needed, ctypes.POINTER(wintypes.DWORD)
                    ).contents.value = size
                    return 0 if buffer is None else result

                with (
                    mock.patch("install.config_windows.sys.platform", "win32"),
                    mock.patch.object(
                        config_windows,
                        "_query",
                        side_effect=query,
                        create=True,
                    ),
                    mock.patch.object(
                        ctypes,
                        "get_last_error",
                        return_value=first_error,
                        create=True,
                    ),
                    mock.patch.object(
                        ctypes,
                        "WinError",
                        return_value=OSError(5, "native refused"),
                        create=True,
                    ),
                    self.assertRaises(OSError),
                ):
                    config_windows._raw_descriptor(77)
