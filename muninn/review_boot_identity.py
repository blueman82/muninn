"""Read supported kernel boot UUIDs without inventing platform evidence."""

from __future__ import annotations

import ctypes
import re
import sys
from pathlib import Path

from muninn.review_pause_state import PauseRefusedError

_UUID = re.compile(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _darwin() -> bytes:
    """Read the bounded read-only boot-session UUID from the kernel."""
    library = ctypes.CDLL(None, use_errno=True)
    query = library.sysctlbyname
    query.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    query.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(64)
    size = ctypes.c_size_t(len(buffer))
    if (
        query(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0)
        != 0
    ):
        raise OSError("boot_identity_unavailable")
    if size.value != 37 or buffer.raw[36:37] != b"\0":
        raise OSError("boot_identity_unavailable")
    return buffer.raw[:36]


def observe() -> str:
    """Return a canonical supported observation or an explicit refusal.

    These adapters do not establish host qualification. Windows repair is
    unavailable until an appropriate boot-lifecycle primitive is established.

    Raises:
        PauseRefusedError: If the boot observation cannot be established.
    """
    try:
        if sys.platform == "linux":
            with Path("/proc/sys/kernel/random/boot_id").open("rb") as handle:
                raw = handle.read(65)
            raw = raw.removesuffix(b"\n")
        elif sys.platform == "darwin":
            raw = _darwin()
        else:
            raise PauseRefusedError("boot_identity_unavailable")
        if _UUID.fullmatch(raw) is None or raw.replace(b"-", b"") == b"0" * 32:
            raise PauseRefusedError("boot_identity_unavailable")
        return sys.platform + ":" + raw.decode("ascii").lower()
    except (OSError, ValueError):
        raise PauseRefusedError("boot_identity_unavailable") from None
