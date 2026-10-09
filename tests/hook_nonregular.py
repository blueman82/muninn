"""Live FIFO or native named-pipe fixtures for refusing transcript reads."""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    _KERNEL: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _create = _KERNEL.CreateNamedPipeW
    _create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _create.restype = ctypes.c_void_p
    _open = _KERNEL.CreateFileW
    _open.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _open.restype = ctypes.c_void_p
    _connect = _KERNEL.ConnectNamedPipe
    _connect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _connect.restype = wintypes.BOOL
    _write = _KERNEL.WriteFile
    _write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _write.restype = wintypes.BOOL
    _close = _KERNEL.CloseHandle
    _close.argtypes = [ctypes.c_void_p]
    _close.restype = wintypes.BOOL


@contextmanager
def nonregular(path: Path, data: bytes = b"{}\n") -> Generator[str]:
    """Provide a real pipe with readable data that must never be consumed.

    Args:
        path: POSIX FIFO path under a synthetic home.
        data: Available bytes, making an accidental pipe read observable.

    Yields:
        Native pipe pathname; it cannot be a regular transcript.

    Raises:
        OSError: If fixture creation fails.
        WinError: If native pipe creation or writes fail.
    """
    if sys.platform != "win32":
        os.mkfifo(path)
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(descriptor, data)
            yield str(path)
        finally:
            os.close(descriptor)
        return
    name = "\\\\.\\pipe\\muninn-test-" + uuid.uuid4().hex
    server = _create(name, 3, 1, 1, 65536, 65536, 0, None)
    if server == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        client = _open(name, 0xC0000000, 0, None, 3, 0, None)
        if client == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _connect(server, None) and ctypes.get_last_error() != 535:
                raise ctypes.WinError(ctypes.get_last_error())
            sent = wintypes.DWORD()
            if not _write(server, data, len(data), ctypes.byref(sent), None):
                raise ctypes.WinError(ctypes.get_last_error())
            yield name
        finally:
            _close(client)
    finally:
        _close(server)
