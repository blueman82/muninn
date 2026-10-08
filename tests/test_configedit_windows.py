"""Actual Windows config edits preserve native ACLs and refuse public files."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from install import config_windows, configedit
from muninn import platform_io
from tests.test_config_windows_metadata import (
    descriptor_difference,
    stage_difference,
)

if sys.platform == "win32":
    import msvcrt

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
                expected = config_windows.security_parts(original)
                stages: dict[str, int] = {}
                original_restore = config_windows._restore
                original_temp = configedit._write_temp
                original_publish = configedit._publish

                def remember(
                    label: str, parts: tuple[int, bytes, bytes, bytes]
                ) -> None:
                    for name, value in stage_difference(
                        expected, parts
                    ).items():
                        stages[f"{label}_{name}"] = value

                def remember_path(label: str, value: Path) -> None:
                    remember(
                        label + "_path",
                        config_windows.security_parts(descriptor(value)),
                    )
                    with platform_io.open_regular(value) as source:
                        remember(
                            label + "_handle",
                            config_windows._read(
                                msvcrt.get_osfhandle(source.fileno())
                            ),
                        )

                def restore(
                    handle: int,
                    owner: ctypes.c_void_p,
                    group: ctypes.c_void_p,
                    dacl: ctypes.c_void_p,
                    value: ctypes.c_void_p,
                ) -> None:
                    remember(
                        "source_descriptor",
                        config_windows.security_parts(
                            config_windows._descriptor_bytes(value)
                        ),
                    )
                    remember(
                        "sibling_before_write_handle",
                        config_windows._read(handle),
                    )
                    original_restore(handle, owner, group, dacl, value)

                def temporary(value: Path, data: bytes, mode: int) -> Path:
                    result = original_temp(value, data, mode)
                    remember_path("sibling_after_close", result)
                    return result

                def publish(source: Path, target: Path) -> None:
                    remember_path("prepublish", source)
                    original_publish(source, target)
                    remember_path("postpublish", target)

                remember_path("original", path)
                with (
                    mock.patch.object(
                        config_windows, "_restore", side_effect=restore
                    ),
                    mock.patch.object(
                        configedit, "_write_temp", side_effect=temporary
                    ),
                    mock.patch.object(
                        configedit, "_publish", side_effect=publish
                    ),
                ):
                    configedit.edit_file(
                        path, lambda data: b"after", lambda before, after: None
                    )
                self.assertEqual(path.read_bytes(), b"after")
                current = descriptor(path)
                self.assertTrue(
                    current == original,
                    json.dumps(
                        {**descriptor_difference(original, current), **stages}
                    ),
                )
                self.assertTrue(platform_io.is_private(path))

        def test_bound_raw_descriptor_matches_path_before_mutation(
            self,
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / "settings.json"
                configedit.atomic_write(path, b"before", 0o600)
                for protected in (False, True):
                    if protected:
                        subprocess.run(
                            ["icacls.exe", str(path), "/inheritance:d"],
                            capture_output=True,
                            check=True,
                        )
                    original = descriptor(path)
                    with platform_io.open_regular(path) as source:
                        raw = config_windows._raw_descriptor(
                            msvcrt.get_osfhandle(source.fileno())
                        )
                    self.assertTrue(
                        raw == original,
                        json.dumps(descriptor_difference(original, raw)),
                    )
                    self.assertEqual(path.read_bytes(), b"before")

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
                self.assertTrue(
                    descriptor(path) == original,
                    "native security descriptor changed",
                )

        def test_protected_dacl_and_explicit_aces_are_preserved(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / "settings.json"
                configedit.atomic_write(path, b"before", 0o600)
                subprocess.run(
                    ["icacls.exe", str(path), "/inheritance:d"],
                    capture_output=True,
                    check=True,
                )
                original = descriptor(path)
                configedit.edit_file(
                    path, lambda data: b"after", lambda before, after: None
                )
                self.assertEqual(path.read_bytes(), b"after")
                self.assertTrue(
                    descriptor(path) == original,
                    "protected security descriptor changed",
                )

        def test_metadata_refusal_keeps_original_bytes_and_acl(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / "settings.json"
                configedit.atomic_write(path, b"before", 0o600)
                original = descriptor(path)
                with (
                    mock.patch.object(
                        config_windows,
                        "_restore",
                        side_effect=PermissionError(
                            "synthetic metadata refusal"
                        ),
                    ),
                    self.assertRaises(OSError),
                ):
                    configedit.edit_file(
                        path,
                        lambda data: b"after",
                        lambda before, after: None,
                    )
                self.assertEqual(path.read_bytes(), b"before")
                self.assertTrue(
                    descriptor(path) == original,
                    "refusal changed security descriptor",
                )
                self.assertEqual(list(path.parent.iterdir()), [path])
