"""Atomic metadata publication preserves old bytes on every refusal."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
            self.assertTrue(publish.call_args.kwargs["retry_move"])
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
        """Bound sharing refusals and publish after the reader closes."""

        def test_held_reader_refuses_boundedly_then_closed_reader_succeeds(
            self,
        ) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                old = path.read_bytes()
                with platform_io.open_regular(path) as handle:
                    started = time.monotonic()
                    with self.assertRaises(PermissionError) as refused:
                        store.write_json_atomic(path, {"pid": 2})
                    self.assertIn(refused.exception.winerror, (5, 32))
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(path.read_bytes(), old)
                    self.assertEqual(list(home.iterdir()), [path])
                    self.assertEqual(json.loads(handle.read()), {"pid": 1})
                store.write_json_atomic(path, {"pid": 2})
                self.assertEqual(json.loads(path.read_bytes()), {"pid": 2})

        def test_short_lived_reader_allows_complete_publication(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                ready, release, closed = (threading.Event() for _ in range(3))
                errors: list[Exception] = []
                failures: list[int] = []
                attempts: list[int] = []
                actual_move = platform_windows._move

                def reader() -> None:
                    try:
                        with platform_io.open_regular(path) as handle:
                            if json.loads(handle.read()) != {"pid": 1}:
                                raise ValueError("reader saw incomplete state")
                            ready.set()
                            if not release.wait(5):
                                raise TimeoutError(
                                    "publication did not release reader"
                                )
                    except Exception as exc:
                        errors.append(exc)
                        ready.set()
                    finally:
                        closed.set()

                def move(source: str, target: str, flags: int) -> int:
                    result = int(actual_move(source, target, flags))
                    attempts.append(result)
                    code = ctypes.get_last_error()
                    if not result:
                        failures.append(code)
                        release.set()
                        if not closed.wait(5):
                            raise TimeoutError("reader did not close")
                        ctypes.set_last_error(code)
                    return result

                thread = threading.Thread(target=reader)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    with mock.patch.object(platform_windows, "_move", move):
                        store.write_json_atomic(path, {"pid": 2})
                    self.assertFalse(errors)
                    self.assertIn(failures[0], (5, 32))
                    self.assertGreaterEqual(len(attempts), 2)
                    self.assertEqual(json.loads(path.read_bytes()), {"pid": 2})
                    self.assertEqual(list(home.iterdir()), [path])
                finally:
                    release.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        def test_unrelated_native_error_never_retries(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                old = path.read_bytes()

                def fail(source: str, target: str, flags: int) -> int:
                    ctypes.set_last_error(3)
                    return 0

                with (
                    mock.patch.object(
                        platform_windows, "_move", side_effect=fail
                    ) as move,
                    self.assertRaises(OSError) as refused,
                ):
                    store.write_json_atomic(path, {"pid": 2})
                self.assertEqual(refused.exception.winerror, 3)
                self.assertEqual(move.call_count, 1)
                self.assertEqual(path.read_bytes(), old)
                self.assertEqual(list(home.iterdir()), [path])

        def test_source_identity_change_refuses_before_second_move(
            self,
        ) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "private"
                platform_io.ensure_private_dir(home)
                path = home / "stop.json"
                store.write_json_atomic(path, {"pid": 1})
                old = path.read_bytes()
                displaced = home / "displaced"

                def fail(source: str, target: str, flags: int) -> int:
                    Path(source).rename(displaced)
                    fd = platform_io.open_private(
                        Path(source), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    )
                    with os.fdopen(fd, "wb") as output:
                        output.write(b"different object")
                    ctypes.set_last_error(5)
                    return 0

                with (
                    mock.patch.object(
                        platform_windows, "_move", side_effect=fail
                    ) as move,
                    self.assertRaisesRegex(OSError, "identity changed"),
                ):
                    store.write_json_atomic(path, {"pid": 2})
                self.assertEqual(move.call_count, 1)
                self.assertEqual(path.read_bytes(), old)
                self.assertEqual(set(home.iterdir()), {path, displaced})

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
