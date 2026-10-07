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
from functools import lru_cache
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt

    _KERNEL: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _SECURITY: Any = ctypes.WinDLL("advapi32", use_last_error=True)

    _VOID = ctypes.c_void_p
    _PTR = ctypes.POINTER(_VOID)
    _DWORD = wintypes.DWORD

    class _AclSize(ctypes.Structure):
        """ACL entry count and byte sizes returned by GetAclInformation."""

        _fields_ = [("count", _DWORD), ("used", _DWORD), ("free", _DWORD)]

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
        _process = _bind(_KERNEL, "GetCurrentProcess", [], _VOID)
        _free = _bind(_KERNEL, "LocalFree", [_VOID], _VOID)
        _token = _bind(
            _SECURITY,
            "OpenProcessToken",
            [_VOID, _DWORD, _PTR],
            wintypes.BOOL,
        )
        _token_info = _bind(
            _SECURITY,
            "GetTokenInformation",
            [_VOID, ctypes.c_int, _VOID, _DWORD, ctypes.POINTER(_DWORD)],
            wintypes.BOOL,
        )
        _sid = _bind(
            _SECURITY,
            "ConvertSidToStringSidW",
            [_VOID, ctypes.POINTER(wintypes.LPWSTR)],
            wintypes.BOOL,
        )
        _security = _bind(
            _SECURITY,
            "GetSecurityInfo",
            [_VOID, ctypes.c_int, _DWORD, _PTR, _PTR, _PTR, _PTR, _PTR],
            _DWORD,
        )
        _acl = _bind(
            _SECURITY,
            "GetAclInformation",
            [_VOID, _VOID, _DWORD, ctypes.c_int],
            wintypes.BOOL,
        )
        _ace = _bind(
            _SECURITY,
            "GetAce",
            [_VOID, _DWORD, _PTR],
            wintypes.BOOL,
        )

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

    def _sid_text(pointer: int | None) -> str:
        """Convert a native SID and free its allocated string."""
        value = wintypes.LPWSTR()
        if not _sid(pointer, ctypes.byref(value)):
            _raise_last()
        try:
            assert value.value is not None
            return value.value
        finally:
            _free(ctypes.cast(value, _VOID))

    @lru_cache(maxsize=1)
    def _user_sid() -> str:
        """Return the process user's SID from its access token."""
        token = _VOID()
        if not _token(_process(), 8, ctypes.byref(token)):
            _raise_last()
        try:
            size = _DWORD()
            _token_info(token, 1, None, 0, ctypes.byref(size))
            data = ctypes.create_string_buffer(size.value)
            if not _token_info(token, 1, data, size, ctypes.byref(size)):
                _raise_last()
            pointer = ctypes.cast(data, _PTR).contents.value
            return _sid_text(pointer)
        finally:
            _close(token)

    def _private(handle: int) -> None:
        """Allow only user, SYSTEM and administrator owners and grants."""
        owner, dacl, descriptor = _VOID(), _VOID(), _VOID()
        error = _security(
            handle,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        try:
            permitted = {_user_sid(), "S-1-5-18", "S-1-5-32-544"}
            # OWNER RIGHTS is safe only after the actual owner is validated.
            if _sid_text(owner.value) not in permitted or not dacl.value:
                raise PermissionError("state ownership or DACL is unsafe")
            size = _AclSize()
            if not _acl(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
                _raise_last()
            for index in range(size.count):
                ace = _VOID()
                if not _ace(dacl, index, ctypes.byref(ace)):
                    _raise_last()
                assert ace.value is not None
                kind = ctypes.c_ubyte.from_address(ace.value).value
                if kind == 1:  # a deny ACE cannot broaden access
                    continue
                if kind != 0 or _sid_text(ace.value + 8) not in (
                    permitted | {"S-1-3-4"}
                ):
                    raise PermissionError("state DACL grants another identity")
        finally:
            _free(descriptor)

    def assert_private(path: Path, *, directory: bool = False) -> None:
        """Check local type and ACLs before reading file contents."""
        absolute = _local_path(path)
        handle = _handle(absolute, 0x20000)
        try:
            _ordinary(handle, directory=directory)
            _private(handle)
        finally:
            _close(handle)

    def private_fd(fd: int) -> None:
        """Check the opened descriptor before writing sensitive bytes."""
        handle = msvcrt.get_osfhandle(fd)
        _ordinary(handle)
        _private(handle)

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
            _private(handle)
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
