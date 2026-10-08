"""Actual Windows config edits preserve native ACLs and refuse public files."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path

from install import configedit
from muninn import platform_io

if sys.platform == "win32":

    def descriptor(path: Path) -> bytes:
        """Read owner, group and DACL bytes from a synthetic regular config.

        Args:
            path: Synthetic fixture path, never a real provider config.

        Returns:
            The self-relative security descriptor for equality checks.

        Raises:
            WinError: If the native descriptor cannot be read.
        """
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        get = security.GetFileSecurityW
        get.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get.restype = wintypes.BOOL
        required = wintypes.DWORD()
        get(str(path), 7, None, 0, ctypes.byref(required))
        value = ctypes.create_string_buffer(required.value)
        if not get(str(path), 7, value, len(value), ctypes.byref(required)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.raw[: required.value]

    class NativeConfigEditTest(unittest.TestCase):
        """Successful edits retain ACLs; refusals retain all original state."""

        def test_native_acl_is_byte_identical_after_replacement(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                parent = Path(tmp).resolve() / "private"
                platform_io.ensure_private_dir(parent)
                path = parent / "space café owner's settings.json"
                configedit.atomic_write(path, b"before", 0o600)
                original = descriptor(path)
                configedit.edit_file(
                    path, lambda data: b"after", lambda before, after: None
                )
                self.assertEqual(path.read_bytes(), b"after")
                self.assertEqual(descriptor(path), original)
                self.assertTrue(platform_io.is_private(path))

        def test_unsafe_config_refusal_keeps_bytes_and_acl(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / "settings.json"
                configedit.atomic_write(path, b"before", 0o600)
                subprocess.run(
                    ["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"],
                    capture_output=True,
                    check=True,
                )
                original = descriptor(path)
                with self.assertRaises(OSError):
                    configedit.edit_file(
                        path, lambda data: b"after", lambda before, after: None
                    )
                self.assertEqual(path.read_bytes(), b"before")
                self.assertEqual(descriptor(path), original)
