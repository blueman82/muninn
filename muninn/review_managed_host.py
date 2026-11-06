"""Read one bounded public Darwin physical identity without a subprocess."""

from __future__ import annotations

import ctypes
import hashlib
import math
import platform
import time
from uuid import RFC_4122, UUID

from muninn.review_json import canonical_json_value
from muninn.review_runtime_models import ManagedHostIdentity


class _Timespec(ctypes.Structure):
    """Match the public Darwin LP64 timespec ABI."""

    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def _wait(deadline: float | None) -> int:
    """Refuse an exhausted caller budget rather than using an infinite wait."""
    if deadline is None:
        return 50_000_000
    if not math.isfinite(deadline):
        raise ValueError("codex_host_identity_unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("codex_host_identity_unavailable")
    nanoseconds = int(min(0.05, remaining) * 1_000_000_000)
    if nanoseconds <= 0:
        raise ValueError("codex_host_identity_unavailable")
    return nanoseconds


def observe(*, deadline: float | None = None) -> ManagedHostIdentity:
    """Return only a domain-separated digest of one checked physical ID."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("codex_platform_changed")
    _wait(deadline)
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        native = library.gethostuuid
        native.argtypes = (
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(_Timespec),
        )
        native.restype = ctypes.c_int
        output = (ctypes.c_ubyte * 16)()
        wait = _Timespec(0, _wait(deadline))
        ctypes.set_errno(0)
        result = native(output, ctypes.byref(wait))
        error = ctypes.get_errno()
        if result != 0 or error != 0:
            raise ValueError("codex_host_identity_unavailable")
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("codex_host_identity_unavailable")
        value = UUID(bytes=bytes(output))
        if value.int == 0 or value.variant != RFC_4122:
            raise ValueError("codex_host_identity_unavailable")
    except (AttributeError, OSError, ValueError, OverflowError):
        raise ValueError("codex_host_identity_unavailable") from None
    raw = canonical_json_value(
        ["muninn-codex-managed-host-v1", "Darwin", "arm64", str(value)]
    ).encode()
    return ManagedHostIdentity(
        kind="darwin-ioplatformuuid-v1",
        digest=hashlib.sha256(raw).hexdigest(),
    )
