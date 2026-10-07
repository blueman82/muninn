"""Win32 file handles, private ACL validation and durable publication.

The APIs are loaded only on Windows. File handles stay open until checks
finish; a pathname-only mode check cannot establish Windows privacy.
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt

    from muninn.platform_windows_security import assert_acl

    _KERNEL: Any = ctypes.WinDLL("kernel32", use_last_error=True)

    _VOID = ctypes.c_void_p
    _DWORD = wintypes.DWORD

    class _Attributes(ctypes.Structure):
        """File attributes and reparse tag returned for an open handle."""

        _fields_ = [("attributes", _DWORD), ("tag", _DWORD)]

    def _bind(lib: Any, name: str, args: list[Any], result: Any) -> Any:
        """Bind the native signature so ctypes preserves full handle values."""
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = result
        return fn

    if sys.platform == "win32":
        _create = _bind(
            _KERNEL,
            "CreateFileW",
            [wintypes.LPCWSTR, _DWORD, _DWORD, _VOID, _DWORD, _DWORD, _VOID],
            _VOID,
        )
        _close = _bind(_KERNEL, "CloseHandle", [_VOID], wintypes.BOOL)
        _type = _bind(_KERNEL, "GetFileType", [_VOID], _DWORD)
        _attrs = _bind(
            _KERNEL,
            "GetFileInformationByHandleEx",
            [_VOID, ctypes.c_int, _VOID, _DWORD],
            wintypes.BOOL,
        )
        _final = _bind(
            _KERNEL,
            "GetFinalPathNameByHandleW",
            [_VOID, wintypes.LPWSTR, _DWORD, _DWORD],
            _DWORD,
        )
        _move = _bind(
            _KERNEL,
            "MoveFileExW",
            [wintypes.LPCWSTR, wintypes.LPCWSTR, _DWORD],
            wintypes.BOOL,
        )
        _long = _bind(
            _KERNEL,
            "GetLongPathNameW",
            [wintypes.LPCWSTR, wintypes.LPWSTR, _DWORD],
            _DWORD,
        )
        _drive = _bind(_KERNEL, "GetDriveTypeW", [wintypes.LPCWSTR], _DWORD)

    def _raise_last() -> None:
        """Raise the calling thread's Win32 error without hiding its code."""
        raise ctypes.WinError(ctypes.get_last_error())

    def _local_path(path: Path) -> Path:
        """Refuse remote devices, reserved names and reparse components.

        Args:
            path: Local pathname to validate without following links.

        Returns:
            Its absolute spelling.

        Raises:
            OSError: If it is not a local disk path or uses a reparse point.
        """
        absolute = path.absolute()
        if not absolute.drive or absolute.drive.startswith("\\\\"):
            raise OSError("a local disk path is required")
        if _drive(absolute.anchor) != 3:
            raise OSError("a fixed local disk is required")
        for part in absolute.parts[1:]:
            if (
                os.path.isreserved(part)
                or part.endswith((".", " "))
                or ":" in part
            ):
                raise OSError("an ordinary local pathname is required")
        for component in (absolute, *absolute.parents):
            try:
                found = component.lstat()
            except FileNotFoundError:
                continue
            if found.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError("reparse paths are refused")
        # DOS aliases and long names address the same file; bind the expected
        # long spelling before opening, then compare the actual handle path.
        buffer = ctypes.create_unicode_buffer(32768)
        length = _long(str(absolute), buffer, len(buffer))
        if length and length < len(buffer):
            return Path(buffer.value)
        if absolute.parent != absolute:
            return _local_path(absolute.parent) / absolute.name
        return absolute

    def _handle(path: Path, access: int) -> int:
        """Open an existing disk object without following its final reparse."""
        handle = _create(str(path), access, 7, None, 3, 0x02200000, None)
        if handle == _VOID(-1).value:
            _raise_last()
        return int(handle)

    def _ordinary(handle: int, *, directory: bool = False) -> None:
        """Reject devices, reparses and a file/directory type mismatch."""
        attrs = _Attributes()
        if _type(handle) != 1 or not _attrs(
            handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)
        ):
            raise OSError("not an inspectable disk file")
        flags = attrs.attributes
        if flags & 0x400 or bool(flags & 0x10) != directory:
            raise OSError("not an ordinary file or directory")

    def assert_private(path: Path, *, directory: bool = False) -> None:
        """Check local type and ACLs before reading file contents."""
        absolute = _local_path(path)
        if directory:
            assert_ancestry(absolute)
        handle = _handle(absolute, 0x20000)
        try:
            _ordinary(handle, directory=directory)
            assert_acl(handle)
        finally:
            _close(handle)

    def private_fd(fd: int) -> None:
        """Check the opened descriptor before writing sensitive bytes."""
        handle = msvcrt.get_osfhandle(fd)
        _ordinary(handle)
        assert_acl(handle)

    def _checked_path(handle: int, expected: Path) -> str:
        """Bind an opened handle to its validated canonical pathname."""
        final = ctypes.create_unicode_buffer(32768)
        length = _final(handle, final, len(final), 0)
        if not length or length >= len(final):
            _raise_last()
        actual = final.value.removeprefix("\\\\?\\")
        if os.path.normcase(actual) != os.path.normcase(str(expected)):
            raise OSError("path changed while opening")
        return actual

    def open_private(path: Path, flags: int) -> int:
        """Create or open private state without following its final reparse.

        Args:
            path: Local state file beneath a validated private directory.
            flags: Non-truncating creation, access and append flags.

        Returns:
            Caller-owned binary descriptor, validated before any write.

        Raises:
            OSError: If identity, ordinary type or privacy cannot be proved.
        """
        absolute = _local_path(path)
        assert_private(absolute.parent, directory=True)
        access = 0x80000000
        if flags & (os.O_WRONLY | os.O_RDWR):
            access |= 0x40000000
        creation = 3
        if flags & os.O_CREAT:
            creation = 1 if flags & os.O_EXCL else 4
        handle = _create(
            str(absolute), access, 7, None, creation, 0x02200000, None
        )
        if handle == _VOID(-1).value:
            _raise_last()
        handle = int(handle)
        try:
            _ordinary(handle)
            assert_acl(handle)
            _checked_path(handle, absolute)
            fd = msvcrt.open_osfhandle(
                handle, os.O_BINARY | (flags & os.O_APPEND)
            )
        except BaseException:
            _close(handle)
            raise
        return fd

    def open_regular(path: Path, root: Path | None = None) -> int:
        """Open verified raw binary data, retaining descriptor identity.

        Args:
            path: Candidate transcript, with no reparse components.
            root: Optional root the final handle's pathname must remain under.

        Returns:
            A binary descriptor owned by the caller.

        Raises:
            OSError: If the path is unsafe, replaced or outside the root.
        """
        absolute = _local_path(path)
        handle = _handle(absolute, 0x80000000)
        try:
            _ordinary(handle)
            actual = _checked_path(handle, absolute)
            if root is not None:
                base = str(_local_path(root))
                if os.path.normcase(os.path.commonpath([base, actual])) != (
                    os.path.normcase(base)
                ):
                    raise OSError("file is outside its root")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _close(handle)
            raise
        return fd

    def publish(source: Path, target: Path, *, replace: bool) -> None:
        """Move a complete private file on disk without copy/delete fallback.

        Args:
            source: Complete synced file, in the target's directory.
            target: Published pathname.
            replace: Allow replacing an existing destination when True.

        Raises:
            OSError: If paths, ACLs or native publication fail.
        """
        source = _local_path(source)
        target = _local_path(target)
        if source.parent != target.parent:
            raise OSError("publication must stay in one directory")
        assert_private(source)
        assert_private(target.parent, directory=True)
        if target.exists():
            assert_private(target)
        if not _move(str(source), str(target), 8 | int(replace)):
            _raise_last()

    def assert_ancestry(path: Path) -> None:
        """Refuse replaceable ancestors before creating private state."""
        absolute = _local_path(path)
        for parent in reversed((absolute, *absolute.parents)):
            if not parent.exists():
                continue
            handle = _handle(parent, 0x20000)
            try:
                _ordinary(handle, directory=True)
                _checked_path(handle, parent)
                assert_acl(handle, policy="ancestor")
            finally:
                _close(handle)

    def assert_executable(path: Path) -> None:
        """Allow public reads but refuse a foreign mutable interpreter."""
        absolute = _local_path(path)
        assert_ancestry(absolute.parent)
        handle = _handle(absolute, 0x20000)
        try:
            _ordinary(handle)
            _checked_path(handle, absolute)
            assert_acl(handle, policy="executable")
        finally:
            _close(handle)

    def assert_interpreter_field(path: Path) -> None:
        """Validate a recorded local path without requiring it to exist."""
        if not path.is_absolute():
            raise OSError("recorded interpreter must be absolute")
        absolute = _local_path(path)
        for candidate in (absolute, *absolute.parents):
            if not os.path.lexists(candidate):
                continue
            handle = _handle(candidate, 0x80)
            try:
                _ordinary(handle, directory=candidate != absolute)
                _checked_path(handle, candidate)
            finally:
                _close(handle)
