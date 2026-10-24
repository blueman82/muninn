"""Observe a persistent macOS volume identifier for guarded repair."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from uuid import UUID

from muninn import review_quiescence_state as state
from muninn.review_quiescence_state import QuiescenceRefusedError

_VOL_UUID = 0x80040000  # ATTR_VOL_INFO | ATTR_VOL_UUID
_RESULT_SIZE = 20


class _AttrList(ctypes.Structure):
    """Match Darwin's fixed five-group attrlist structure."""

    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


def observe(home: Path) -> str:
    """Read the home volume UUID through a checked directory descriptor.

    Returns:
        The canonical lowercase UUID of the containing volume.

    Raises:
        QuiescenceRefusedError: If the volume has no usable native UUID.
    """
    if sys.platform != "darwin":
        raise QuiescenceRefusedError("quiescence_unverified")
    try:
        before = state.directory_identity(home)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(home, flags)
        try:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != before:
                raise QuiescenceRefusedError("quiescence_unverified")
            native = ctypes.CDLL(None, use_errno=True).fgetattrlist
            native.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(_AttrList),
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_ulong,
            ]
            native.restype = ctypes.c_int
            attrs = _AttrList(bitmapcount=5, volattr=_VOL_UUID)
            buffer = ctypes.create_string_buffer(_RESULT_SIZE)
            result = native(fd, ctypes.byref(attrs), buffer, len(buffer), 0)
            if (
                result != 0
                or int.from_bytes(buffer.raw[:4], sys.byteorder)
                != _RESULT_SIZE
            ):
                raise QuiescenceRefusedError("quiescence_unverified")
            identifier = UUID(bytes=buffer.raw[4:_RESULT_SIZE])
            current = os.fstat(fd)
            if (
                identifier.int == 0
                or (current.st_dev, current.st_ino) != before
                or state.directory_identity(home) != before
            ):
                raise QuiescenceRefusedError("quiescence_unverified")
            return str(identifier)
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError):
        raise QuiescenceRefusedError("quiescence_unverified") from None
