"""Handle-bound Windows ACL checks for private state and trusted ancestry."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from functools import lru_cache
from typing import Any, Literal

if sys.platform == "win32":
    _KERNEL: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _SECURITY: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    _VOID = ctypes.c_void_p
    _PTR = ctypes.POINTER(_VOID)
    _DWORD = wintypes.DWORD

    class _AclSize(ctypes.Structure):
        """ACL entry count and byte sizes returned by GetAclInformation."""

        _fields_ = [("count", _DWORD), ("used", _DWORD), ("free", _DWORD)]

    def _bind(lib: Any, name: str, args: list[Any], result: Any) -> Any:
        """Declare native argument widths and return values."""
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
        return fn

    _close = _bind(_KERNEL, "CloseHandle", [_VOID], wintypes.BOOL)
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
    _lookup = _bind(
        _SECURITY,
        "LookupAccountNameW",
        [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            _VOID,
            ctypes.POINTER(_DWORD),
            wintypes.LPWSTR,
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
        ],
        wintypes.BOOL,
    )

    def _raise_last() -> None:
        """Preserve the native failure code."""
        raise ctypes.WinError(ctypes.get_last_error())

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

    def assert_acl(
        handle: int,
        *,
        policy: Literal["private", "ancestor", "executable"] = "private",
    ) -> None:
        """Validate ownership and grants using one closed access policy."""
        if policy not in {"private", "ancestor", "executable"}:
            raise ValueError("unknown ACL policy")
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
            if policy != "private":
                permitted.add(_installer_sid())
            # OWNER RIGHTS is safe only after the actual owner is validated.
            if _sid_text(owner.value) not in permitted or not dacl.value:
                raise PermissionError("state ownership or DACL is unsafe")
            _assert_entries(dacl, permitted, policy)
        finally:
            _free(descriptor)

    @lru_cache(maxsize=1)
    def _installer_sid() -> str:
        """Resolve the exact local Windows servicing identity or refuse."""
        size, domain_size, kind = _DWORD(), _DWORD(), _DWORD()
        name = "NT SERVICE\\TrustedInstaller"
        _lookup(
            None,
            name,
            None,
            ctypes.byref(size),
            None,
            ctypes.byref(domain_size),
            ctypes.byref(kind),
        )
        if not size.value or not domain_size.value:
            _raise_last()
        sid = ctypes.create_string_buffer(size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value)
        if not _lookup(
            None,
            name,
            sid,
            ctypes.byref(size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(kind),
        ):
            _raise_last()
        if domain.value.casefold() != "nt service":
            raise PermissionError("unverifiable servicing identity")
        return _sid_text(ctypes.addressof(sid))

    def _assert_entries(
        dacl: ctypes.c_void_p,
        permitted: set[str],
        policy: Literal["private", "ancestor", "executable"],
    ) -> None:
        """Refuse unsafe effective access rules and unknown ACE shapes."""
        size = _AclSize()
        if not _acl(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            _raise_last()
        for index in range(size.count):
            ace = _VOID()
            if not _ace(dacl, index, ctypes.byref(ace)):
                _raise_last()
            assert ace.value is not None
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            if kind not in {0, 1}:
                raise PermissionError("unknown DACL entry")
            if policy != "private" and flags & 8:
                continue
            if kind == 1:  # a deny ACE cannot broaden access
                continue
            identity = _sid_text(ace.value + 8)
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            dangerous = 0x100D0040
            if policy == "executable":
                dangerous |= 0x40000116
            if identity not in permitted | {"S-1-3-4"} and (
                policy == "private" or mask & dangerous
            ):
                raise PermissionError("unsafe foreign DACL grant")
