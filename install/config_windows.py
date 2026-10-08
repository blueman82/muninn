"""Create provider config replacement files with the original native ACL."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

from muninn import platform_windows
from muninn.platform_io import assert_private_fd, open_regular

if sys.platform == "win32":
    import msvcrt

    _KERNEL: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _SECURITY: Any = ctypes.WinDLL("advapi32", use_last_error=True)

    class _Attributes(ctypes.Structure):
        """Security descriptor supplied before creating a replacement file."""

        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    _get = _SECURITY.GetSecurityInfo
    _get.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _get.restype = wintypes.DWORD
    _set = _SECURITY.SetSecurityInfo
    _set.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _set.restype = wintypes.DWORD
    _length = _SECURITY.GetSecurityDescriptorLength
    _length.argtypes = [ctypes.c_void_p]
    _length.restype = wintypes.DWORD
    _create = _KERNEL.CreateFileW
    _create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Attributes),
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _create.restype = ctypes.c_void_p
    _free = _KERNEL.LocalFree
    _free.argtypes = [ctypes.c_void_p]
    _free.restype = ctypes.c_void_p
    _close = _KERNEL.CloseHandle
    _close.argtypes = [ctypes.c_void_p]
    _close.restype = wintypes.BOOL


class SecurityMismatchError(PermissionError):
    """Refuse changed native metadata while exposing only numeric codes."""

    def __init__(
        self,
        component: str,
        before: tuple[int, bytes, bytes, bytes],
        after: tuple[int, bytes, bytes, bytes],
    ) -> None:
        """Capture controls and exact component equality without raw bytes.

        Args:
            component: Fixed control, owner, group or DACL failure code.
            before: Original descriptor-bound security components.
            after: Empty sibling's descriptor-bound security components.

        Raises:
            ValueError: If the component code is unsupported.
        """
        if component not in {"control", "owner", "group", "dacl"}:
            raise ValueError("unsupported config security component")
        super().__init__("config security differs: " + component)
        self.control_before = before[0]
        self.control_after = after[0]
        self.owner_equal = int(before[1] == after[1])
        self.group_equal = int(before[2] == after[2])
        self.dacl_equal = int(before[3] == after[3])


def security_parts(raw: bytes) -> tuple[int, bytes, bytes, bytes]:
    """Validate self-relative owner, group, control and ordered DACL bytes.

    Args:
        raw: Descriptor returned by the native security API.

    Returns:
        Exact control flags and security components independent of offsets.

    Raises:
        OSError: If the descriptor is truncated or omits required security.
    """
    if len(raw) < 20:
        raise OSError("invalid config security descriptor")
    revision, _, control, owner, group, _, dacl = struct.unpack(
        "<BBHLLLL", raw[:20]
    )
    if revision != 1 or not control & 0x8000 or not control & 4:
        raise OSError("invalid config security descriptor")
    parts: list[bytes] = []
    for offset in (owner, group, dacl):
        if offset < 20 or offset + 8 > len(raw):
            raise OSError("missing config security component")
        if offset == dacl:
            length = int.from_bytes(raw[offset + 2 : offset + 4], "little")
        else:
            if raw[offset] != 1 or raw[offset + 1] > 15:
                raise OSError("invalid config security SID")
            length = 8 + 4 * raw[offset + 1]
        if length < 8 or offset + length > len(raw):
            raise OSError("truncated config security component")
        parts.append(raw[offset : offset + length])
    return control, parts[0], parts[1], parts[2]


def _descriptor_bytes(descriptor: ctypes.c_void_p) -> bytes:
    """Copy a validated native descriptor without printing its contents."""
    if sys.platform != "win32" or descriptor.value is None:
        raise OSError("native config descriptor unavailable")
    length = int(_length(descriptor))
    if not 20 <= length <= 1048576:
        raise OSError("invalid config security descriptor size")
    return ctypes.string_at(descriptor, length)


def _restore(
    handle: int,
    owner: ctypes.c_void_p,
    group: ctypes.c_void_p,
    dacl: ctypes.c_void_p,
    descriptor: ctypes.c_void_p,
) -> None:
    """Restore original metadata on only the empty sibling before any bytes."""
    if sys.platform != "win32" or not all(
        (owner.value, group.value, dacl.value)
    ):
        raise OSError("native config security component unavailable")
    wanted = security_parts(_descriptor_bytes(descriptor))
    protection = 0x80000000 if wanted[0] & 0x1000 else 0x20000000
    error = _set(handle, 1, 7 | protection, owner, group, dacl, None)
    if error:
        raise ctypes.WinError(error)
    actual = ctypes.c_void_p()
    error = _get(handle, 1, 7, None, None, None, None, ctypes.byref(actual))
    if error:
        raise ctypes.WinError(error)
    try:
        got = security_parts(_descriptor_bytes(actual))
        for code, before, after in zip(
            ("control", "owner", "group", "dacl"), wanted, got, strict=True
        ):
            if before != after:
                raise SecurityMismatchError(code, wanted, got)
    finally:
        _free(actual)


def replacement(path: Path) -> tuple[int, Path]:
    """Create an empty sibling with source owner, group and DACL.

    Args:
        path: Existing private provider config under a private parent.

    Returns:
        A caller-owned writable binary descriptor and temporary pathname.

    Raises:
        OSError: If identity or privacy validation fails.
        WinError: If native security creation fails.
    """
    if sys.platform != "win32":
        raise OSError("native ACL replacement is Windows-only")
    platform_windows.assert_ancestry(path.parent)
    platform_windows.assert_private(path.parent, directory=True)
    owner, group, dacl, descriptor = (ctypes.c_void_p() for _ in range(4))
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    with open_regular(path, expected=path.lstat()) as source:
        assert_private_fd(source.fileno())
        result = _get(
            msvcrt.get_osfhandle(source.fileno()),
            1,
            7,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise ctypes.WinError(result)
        try:
            attributes = _Attributes(
                ctypes.sizeof(_Attributes), descriptor, False
            )
            handle = _create(
                str(tmp),
                0xC00C0000,
                7,
                ctypes.byref(attributes),
                1,
                0x02200000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(
                    int(handle), os.O_BINARY | os.O_RDWR
                )
            except BaseException:
                _close(handle)
                tmp.unlink(missing_ok=True)
                raise
            try:
                assert_private_fd(fd)
                if os.fstat(fd).st_size:
                    raise OSError("config replacement is not empty")
                with open_regular(tmp, expected=os.fstat(fd)):
                    pass
                _restore(int(handle), owner, group, dacl, descriptor)
            except BaseException:
                os.close(fd)
                tmp.unlink(missing_ok=True)
                raise
        finally:
            _free(descriptor)
    try:
        assert_private_fd(fd)
        with open_regular(tmp, expected=os.fstat(fd)):
            pass
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return fd, tmp
