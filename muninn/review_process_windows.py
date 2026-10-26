"""Create a CLI atomically inside a wrapper-exclusive kill-on-close Job."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

if sys.platform == "win32":
    import msvcrt

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p


class _BasicLimits(ctypes.Structure):
    """Match JOBOBJECT_BASIC_LIMIT_INFORMATION on the native architecture."""

    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", _DWORD),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active", _DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", _DWORD),
        ("scheduling", _DWORD),
    ]


class JobLimits(ctypes.Structure):
    """Match extended limits without enabling either breakaway flag."""

    _fields_ = [
        ("basic", _BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Startup(ctypes.Structure):
    """Match STARTUPINFOW and explicitly provide only three pipe handles."""

    _fields_ = [
        ("size", _DWORD),
        ("reserved", ctypes.c_wchar_p),
        ("desktop", ctypes.c_wchar_p),
        ("title", ctypes.c_wchar_p),
        ("geometry", _DWORD * 7),
        ("flags", _DWORD),
        ("show", ctypes.c_uint16),
        ("reserved_size", ctypes.c_uint16),
        ("reserved_bytes", _HANDLE),
        ("stdin", _HANDLE),
        ("stdout", _HANDLE),
        ("stderr", _HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    """Append the process-creation attribute list to STARTUPINFOW."""

    _fields_ = [("startup", _Startup), ("attributes", _HANDLE)]


class _ProcessInfo(ctypes.Structure):
    """Receive owned process/thread handles and their native identifiers."""

    _fields_ = [
        ("process", _HANDLE),
        ("thread", _HANDLE),
        ("pid", _DWORD),
        ("tid", _DWORD),
    ]


def _kernel() -> Any:
    """Bind full-width Win32 signatures or refuse the unavailable host."""
    if sys.platform != "win32":
        raise OSError("atomic_job_unavailable")
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "CreateJobObjectW": ([_HANDLE, ctypes.c_wchar_p], _HANDLE),
        "SetInformationJobObject": (
            [_HANDLE, _DWORD, _HANDLE, _DWORD],
            ctypes.c_int,
        ),
        "CloseHandle": ([_HANDLE], ctypes.c_int),
        "InitializeProcThreadAttributeList": (
            [_HANDLE, _DWORD, _DWORD, _HANDLE],
            ctypes.c_int,
        ),
        "UpdateProcThreadAttribute": (
            [
                _HANDLE,
                _DWORD,
                ctypes.c_size_t,
                _HANDLE,
                ctypes.c_size_t,
                _HANDLE,
                _HANDLE,
            ],
            ctypes.c_int,
        ),
        "DeleteProcThreadAttributeList": ([_HANDLE], None),
        "CreateProcessW": (
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                _HANDLE,
                _HANDLE,
                ctypes.c_int,
                _DWORD,
                _HANDLE,
                ctypes.c_wchar_p,
                _HANDLE,
                _HANDLE,
            ],
            ctypes.c_int,
        ),
        "GetExitCodeProcess": ([_HANDLE, _HANDLE], ctypes.c_int),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def create_job(kernel: Any) -> int:
    """Create a noninheritable Job whose sole owner is the finite wrapper."""
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("atomic_job_unavailable")
    limits = JobLimits()
    limits.basic.flags = 0x2000
    if not kernel.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        kernel.CloseHandle(job)
        raise OSError("atomic_job_unavailable")
    return job


@contextmanager
def attributes(
    kernel: Any, job: int, handles: tuple[int, int, int]
) -> Generator[Any]:
    """Assign Job membership and descriptor exclusion in the creation call."""
    size = ctypes.c_size_t()
    kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    memory = ctypes.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(
        memory, 2, 0, ctypes.byref(size)
    ):
        raise OSError("atomic_job_unavailable")
    job_list = (_HANDLE * 1)(job)
    handle_list = (_HANDLE * 3)(*handles)
    try:
        for key, value in ((0x2000D, job_list), (0x20002, handle_list)):
            if not kernel.UpdateProcThreadAttribute(
                memory,
                0,
                key,
                ctypes.byref(value),
                ctypes.sizeof(value),
                None,
                None,
            ):
                raise OSError("atomic_job_unavailable")
        yield memory
    finally:
        kernel.DeleteProcThreadAttributeList(memory)


class NativeChild:
    """Keep Windows native CLI handles private to its wrapper's Job."""

    def __init__(
        self,
        kernel: Any,
        job: int,
        process: int,
        pid: int,
        pipes: tuple[BinaryIO, BinaryIO, BinaryIO],
    ) -> None:
        """Adopt the atomically created member and parent pipe endpoints."""
        self.kernel, self.job, self.process, self.pid = (
            kernel,
            job,
            process,
            pid,
        )
        self.stdin, self.stdout, self.stderr = pipes

    def poll(self) -> int | None:
        """Read CLI exit status without weakening wrapper Job ownership."""
        status = _DWORD()
        if not self.kernel.GetExitCodeProcess(
            self.process, ctypes.byref(status)
        ):
            raise OSError("native_process_unavailable")
        return None if status.value == 259 else status.value

    def close_job(self) -> None:
        """Contain descendants while retaining pipes for a final drain."""
        if self.job:
            if not self.kernel.CloseHandle(self.job):
                raise OSError("native_job_close_failed")
            self.job = 0

    def close(self) -> None:
        """Close the sole Job first so live descendants cannot survive."""
        self.close_job()
        self.kernel.CloseHandle(self.process)
        for pipe in (self.stdin, self.stdout, self.stderr):
            pipe.close()


def _handle(fd: int) -> int:
    """Read the native handle only on a supported Windows host."""
    if sys.platform == "win32":
        return msvcrt.get_osfhandle(fd)
    raise OSError("atomic_job_unavailable")


def launch(
    argv: tuple[str, ...], cwd: Path, env: dict[str, str]
) -> NativeChild:
    """Launch once with atomic Job membership."""
    kernel = _kernel()
    job = create_job(kernel)
    descriptors: list[int] = []
    information = _ProcessInfo()
    try:
        for _ in range(3):
            descriptors.extend(os.pipe())
        child_fds = (descriptors[0], descriptors[3], descriptors[5])
        for fd in child_fds:
            os.set_inheritable(fd, True)
        handles = (
            _handle(child_fds[0]),
            _handle(child_fds[1]),
            _handle(child_fds[2]),
        )
        with attributes(kernel, job, handles) as attribute_list:
            startup = _StartupEx()
            startup.startup.size = ctypes.sizeof(startup)
            startup.startup.flags = 0x100
            (
                startup.startup.stdin,
                startup.startup.stdout,
                startup.startup.stderr,
            ) = handles
            startup.attributes = ctypes.cast(attribute_list, _HANDLE)
            command = ctypes.create_unicode_buffer(
                subprocess.list2cmdline(argv)
            )
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0"
            )
            if not kernel.CreateProcessW(
                argv[0],
                command,
                None,
                None,
                True,
                0x80000 | 0x400,
                environment,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            ):
                raise OSError("atomic_job_unavailable")
        kernel.CloseHandle(information.thread)
        for fd in child_fds:
            os.close(fd)
            descriptors.remove(fd)
        pipes = (
            os.fdopen(descriptors[0], "wb", buffering=0),
            os.fdopen(descriptors[1], "rb", buffering=0),
            os.fdopen(descriptors[2], "rb", buffering=0),
        )
        descriptors.clear()
        return NativeChild(
            kernel, job, information.process, information.pid, pipes
        )
    except BaseException:
        kernel.CloseHandle(job)
        if information.process:
            kernel.CloseHandle(information.process)
        for fd in descriptors:
            os.close(fd)
        raise
