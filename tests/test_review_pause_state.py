"""Verify durable owner evidence before exposing governed material."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from unittest import mock

from muninn import review_pause_state as state
from muninn import store
from tests.review_pause_support import initialize
from tests.store_support import public_read


class PauseStateTests(unittest.TestCase):
    """Treat an uncertain predecessor as a refusal, never a fresh owner."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_active_complete_reuses_exact_lock_and_token(self) -> None:
        initialize(self.home)
        identity = (self.home / state.LOCK).stat().st_ino
        fd, token = state.begin_owner(self.home)
        try:
            witness = state.parse(
                state.inspect(self.home, state.WITNESS)[0], state.Witness
            )
            assert witness is not None and witness.owner is not None
            self.assertEqual(
                (witness.owner.pid, witness.owner.generation),
                (os.getpid(), token),
            )
            self.assertEqual(witness.state, "active")
            with self.assertRaises(BlockingIOError):
                state.acquire(self.home)
            state.complete_owner(self.home, fd, token)
            self.assertEqual((self.home / state.LOCK).stat().st_ino, identity)
        finally:
            os.close(fd)
        self.assertEqual(
            json.loads((self.home / state.WITNESS).read_bytes())["state"],
            "complete",
        )

    def test_uncertain_predecessor_never_gets_overwritten(self) -> None:
        for raw in (b"", b"not-json", b"x" * 2048):
            with self.subTest(raw=raw[:8]):
                path = self.home / state.WITNESS
                path.write_bytes(raw)
                path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "pause_unverified"):
                    state.begin_owner(self.home)
                self.assertEqual(path.read_bytes(), raw)

    def test_abandoned_active_is_not_reseeded_or_laundered(self) -> None:
        initialize(self.home)
        fd, _ = state.begin_owner(self.home)
        os.close(fd)

        before = (self.home / state.WITNESS).read_bytes()
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.begin_owner(self.home)
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            initialize(self.home)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)

    def test_strict_bounded_record_rejects_coercions_and_duplicate_keys(
        self,
    ) -> None:
        initialize(self.home)
        good = (self.home / state.WITNESS).read_bytes()
        value = json.loads(good)
        cases = [
            good + b"{}",
            b" " * 1025,
            good.replace(b'"version": 1', b'"version": true'),
        ]
        cases.extend(
            json.dumps(value | change).encode()
            for change in (
                {"extra": 1},
                {"lock_identity": [True, 2]},
                {"state": "active"},
            )
        )
        cases.append(
            good.replace(b'"version": 1', b'"version": 1, "version": 1')
        )
        for raw in cases:
            self.assertIsNone(state.parse(raw, state.Witness))

    def test_unsafe_witness_or_lock_cannot_be_silently_privatized(
        self,
    ) -> None:
        initialize(self.home)
        for name in (state.WITNESS, state.LOCK):
            path = self.home / name
            with (
                public_read(self, path),
                self.assertRaisesRegex(ValueError, "pause_unverified"),
            ):
                state.begin_owner(self.home)
        path = self.home / state.WITNESS
        target = self.home / "target"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.begin_owner(self.home)

    def test_completion_requires_same_token_and_lock_inode(self) -> None:
        initialize(self.home)
        fd, token = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.complete_owner(self.home, fd, "a" * 32)
        path = self.home / state.LOCK
        path.rename(self.home / "old-lock")
        path.touch(mode=0o600)
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.complete_owner(self.home, fd, token)

    @unittest.skipIf(os.name == "nt", "Windows refuses baseline retirement")
    def test_baseline_is_synced_away_before_active_publication(self) -> None:
        initialize(self.home)
        path = self.home / state.BASELINE
        store.write_json_atomic(path, {"obsolete": True})
        original = state.publish

        def check(
            home: Path, fd: int, name: str, value: state.StrictModel
        ) -> None:
            self.assertFalse(path.exists())
            original(home, fd, name, value)

        with mock.patch.object(state, "publish", side_effect=check):
            fd, token = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        state.complete_owner(self.home, fd, token)

    @unittest.skipIf(os.name == "nt", "Windows refuses baseline retirement")
    def test_failed_retirement_sync_never_publishes_active(self) -> None:
        initialize(self.home)
        before = (self.home / state.WITNESS).read_bytes()
        store.write_json_atomic(self.home / state.BASELINE, {"obsolete": True})
        with (
            mock.patch.object(
                state.file_sync, "sync_path", side_effect=OSError("sync")
            ),
            self.assertRaises(OSError),
        ):
            state.begin_owner(self.home)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)

    def test_windows_baseline_preserved_and_absent_path_still_operates(
        self,
    ) -> None:
        initialize(self.home)
        path = self.home / state.BASELINE
        store.write_json_atomic(path, {"unexpected": True})
        before = path.read_bytes(), (self.home / state.WITNESS).read_bytes()
        windows = SimpleNamespace(**(vars(os) | {"name": "nt"}))
        with (
            mock.patch.object(state, "os", windows),
            self.assertRaisesRegex(ValueError, "pause_unverified"),
        ):
            state.begin_owner(self.home)
        self.assertEqual(
            (path.read_bytes(), (self.home / state.WITNESS).read_bytes()),
            before,
        )
        path.unlink()
        with (
            mock.patch.object(state, "os", windows),
            mock.patch.object(
                state.store, "write_json_atomic", wraps=store.write_json_atomic
            ),
        ):
            fd, _ = state.begin_owner(self.home)
        os.close(fd)

    def test_baseline_rejects_malformed_or_nil_boot_uuid(self) -> None:
        for boot in (
            "linux:" + "a" * 36,
            "darwin:00000000-0000-0000-0000-000000000000",
        ):
            raw = json.dumps(
                {
                    "version": 1,
                    "lock_identity": [1, 2],
                    "boot": boot,
                    "predecessor": None,
                }
            ).encode()
            self.assertIsNone(state.parse(raw, state.Baseline))

    def test_baseline_rejects_malformed_or_nil_volume_uuid(self) -> None:
        """A stored volume identifier must be canonical and nonzero."""
        for volume_uuid in (
            "F5A5FB75-3AF2-4F3F-A7B2-5744E5A0B339",
            "00000000-0000-0000-0000-000000000000",
            "not-a-uuid",
        ):
            raw = json.dumps(
                {
                    "version": 1,
                    "lock_identity": [1, 2],
                    "boot": "darwin:12345678-1234-4234-8234-123456789abc",
                    "predecessor": None,
                    "volume_uuid": volume_uuid,
                }
            ).encode()
            with self.subTest(volume_uuid=volume_uuid):
                self.assertIsNone(state.parse(raw, state.Baseline))

    def test_active_publication_failure_never_exposes_material(self) -> None:
        initialize(self.home)
        original = state.publish

        def visible(
            home: Path, fd: int, name: str, value: state.StrictModel
        ) -> None:
            original(home, fd, name, value)
            raise OSError("active sync failure")

        with (
            mock.patch.object(state, "publish", side_effect=visible),
            self.assertRaises(OSError),
        ):
            state.begin_owner(self.home)
        self.assertEqual(
            json.loads((self.home / state.WITNESS).read_bytes())["state"],
            "active",
        )
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            state.begin_owner(self.home)

    def test_inspection_rejects_mutation_during_streaming_hash(self) -> None:
        initialize(self.home)
        path = self.home / state.WITNESS
        original = state.platform_io.open_regular

        @contextlib.contextmanager
        def changing(
            target: Path, *, root: Path, expected: os.stat_result
        ) -> Generator[BinaryIO]:
            with original(target, root=root, expected=expected) as handle:
                proxy = mock.Mock(wraps=handle)

                def read(size: int) -> bytes:
                    raw = handle.read(size)
                    if raw:
                        with path.open("ab") as output:
                            output.write(b" ")
                        proxy.read.side_effect = handle.read
                    return raw

                proxy.read.side_effect = read
                yield cast(BinaryIO, proxy)

        with (
            mock.patch.object(
                state.platform_io, "open_regular", side_effect=changing
            ),
            self.assertRaisesRegex(ValueError, "pause_unverified"),
        ):
            state.inspect(self.home, state.WITNESS)
