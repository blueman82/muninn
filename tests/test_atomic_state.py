"""Atomic metadata publication preserves old bytes on every refusal."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import platform_io, platform_windows, store


class AtomicStateTests(unittest.TestCase):
    """Use the same native publication contract as private lifecycle state."""

    def test_windows_uses_validated_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.json"
            with (
                mock.patch.object(store.os, "name", "nt"),
                mock.patch.object(store, "Path", type(path)),
                mock.patch.object(
                    platform_windows, "publish", create=True
                ) as publish,
            ):
                store.write_json_atomic(path, {"pid": 123})
            publish.assert_called_once()
            source, target = publish.call_args.args
            self.assertEqual(target, path)
            self.assertEqual(json.loads(source.read_bytes()), {"pid": 123})
            source.unlink()

    def test_permanent_native_failure_preserves_old_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stop.json"
            path.write_bytes(b"original")
            with (
                mock.patch.object(store.os, "name", "nt"),
                mock.patch.object(store, "Path", type(path)),
                mock.patch.object(
                    platform_windows,
                    "publish",
                    create=True,
                    side_effect=PermissionError("publication refused"),
                ),
                self.assertRaises(PermissionError),
            ):
                store.write_json_atomic(path, {"pid": 123})
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])


if sys.platform == "win32":

    class NativeAtomicStateTests(unittest.TestCase):
        """Publish complete state while the delete-shared reader lives."""

        def test_held_shared_reader_allows_complete_publication(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                with platform_io.open_regular(path) as handle:
                    for pid in range(2, 102):
                        store.write_json_atomic(path, {"pid": pid})
                        self.assertEqual(
                            json.loads(path.read_bytes()), {"pid": pid}
                        )
                    self.assertEqual(json.loads(handle.read()), {"pid": 1})

        def test_unsafe_acl_refuses_before_publication(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                old = path.read_bytes()
                subprocess.run(
                    ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
                    check=True,
                    capture_output=True,
                )
                with (
                    mock.patch.object(platform_windows, "_move") as move,
                    self.assertRaises(PermissionError),
                ):
                    store.write_json_atomic(path, {"pid": 2})
                move.assert_not_called()
                self.assertEqual(path.read_bytes(), old)
