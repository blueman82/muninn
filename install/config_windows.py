"""Create provider config replacement files with the original native ACL."""

from __future__ import annotations

import ctypes
import os
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
    platform_windows.assert_ancestry(path)
    platform_windows.assert_private(path.parent, directory=True)
    descriptor = ctypes.c_void_p()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    with open_regular(path, expected=path.lstat()) as source:
        assert_private_fd(source.fileno())
        result = _get(
            msvcrt.get_osfhandle(source.fileno()),
            1,
            7,
            None,
            None,
            None,
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
                0xC0000000,
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
