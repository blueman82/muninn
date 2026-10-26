"""Read native process creation identity without spawning or reaping."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from pathlib import Path
from typing import Any

from muninn.review_models import ProcessIdentity
from muninn.review_process_posix import sole_zombie as sole_zombie


class _BsdInfo(ctypes.Structure):
    """Match Darwin proc_bsdinfo's fixed-width public native layout."""

    _fields_ = [
        ("prefix", ctypes.c_uint32 * 12),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("files", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32),
        ("device", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("seconds", ctypes.c_uint64),
        ("micros", ctypes.c_uint64),
    ]


def _darwin(pid: int) -> str:
    """Read creation time from libproc without releasing child identity."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    info = _BsdInfo()
    query = library.proc_pidinfo
    query.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    query.restype = ctypes.c_int
    if query(
        pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
    ) != ctypes.sizeof(info):
        raise ProcessLookupError("process_identity_unverified")
    return f"darwin:{info.seconds}:{info.micros}"


def _linux(pid: int) -> str:
    """Bind Linux boot identity and process start ticks, including zombies."""
    with (Path("/proc") / str(pid) / "stat").open("rb", buffering=0) as handle:
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise OSError("process_identity_unverified")
    fields = raw.rsplit(b")", 1)[1].split()
    ticks = int(fields[19])
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return f"linux:{boot}:{ticks}"


def _windows(pid: int) -> str:
    """Read process creation FILETIME using a noninheritable query handle."""
    if sys.platform != "win32":
        raise OSError("process_identity_unverified")
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("process_identity_unverified")
    times = [ctypes.c_uint64() for _ in range(4)]
    try:
        if not kernel.GetProcessTimes(
            handle, *(ctypes.byref(t) for t in times)
        ):
            raise OSError("process_identity_unverified")
        return f"windows:{times[0].value}"
    finally:
        kernel.CloseHandle(handle)


def identify(pid: int) -> ProcessIdentity:
    """Require current native creation identity and Unix group membership."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("process_identity_unverified")
    if sys.platform == "darwin":
        created = _darwin(pid)
    elif sys.platform == "linux":
        created = _linux(pid)
    elif sys.platform == "win32":
        created = _windows(pid)
    else:
        raise OSError("process_identity_unverified")
    return ProcessIdentity(
        pid=pid,
        created=created,
        pgid=None if os.name == "nt" else os.getpgid(pid),
        job_marker=None,
    )


def recover_group(previous: ProcessIdentity) -> None:
    """Refuse signals unless current native identity matches."""
    try:
        current = identify(previous.pid)
    except OSError:
        raise ValueError("process_identity_unverified") from None
    if os.name == "nt" or current != previous or current.pgid != current.pid:
        raise ValueError("process_identity_unverified")
    os.killpg(current.pid, signal.SIGKILL)
