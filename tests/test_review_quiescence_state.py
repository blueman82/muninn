"""Verify durable owner evidence before exposing governed material."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from unittest import mock

from muninn import cli_serve, poller_stop, review_supervisor, store
from muninn import review_quiescence_state as state
from tests.ingest_support import TID
from tests.review_quiescence_support import initialize, interrupt_erasure
from tests.test_review_supervisor import SupervisorCase
from tests.test_review_worker import policy


class QuiescenceStateTests(unittest.TestCase):
    """Treat an uncertain predecessor as a refusal, never a fresh owner."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_missing_witness_refuses_before_loading_review_material(
        self,
    ) -> None:
        selected = policy().model_copy(
            update={"projection_version": "permitted-fields-v2"}
        )
        supervisor = review_supervisor.Supervisor(self.home, selected)
        with (
            mock.patch.object(
                review_supervisor,
                "load_capability",
                side_effect=AssertionError("material exposed"),
            ),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            supervisor.start(1.0)
        self.assertFalse((self.home / "review.supervisor.json").exists())

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
                with self.assertRaisesRegex(
                    ValueError, "quiescence_unverified"
                ):
                    state.begin_owner(self.home)
                self.assertEqual(path.read_bytes(), raw)

    def test_abandoned_active_is_not_reseeded_or_laundered(self) -> None:
        initialize(self.home)
        fd, _ = state.begin_owner(self.home)
        os.close(fd)

        before = (self.home / state.WITNESS).read_bytes()
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
            state.begin_owner(self.home)
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
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
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
                state.begin_owner(self.home)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            path.chmod(0o600)
        path = self.home / state.WITNESS
        target = self.home / "target"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
            state.begin_owner(self.home)

    def test_completion_requires_same_token_and_lock_inode(self) -> None:
        initialize(self.home)
        fd, token = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
            state.complete_owner(self.home, fd, "a" * 32)
        path = self.home / state.LOCK
        path.rename(self.home / "old-lock")
        path.touch(mode=0o600)
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
            state.complete_owner(self.home, fd, token)

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
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
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
        with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
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
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            state.inspect(self.home, state.WITNESS)


class OwnerCompletionTests(SupervisorCase):
    """Require real containment and retained-reference cleanup before proof."""

    def test_restart_replays_interrupted_intent_before_material(self) -> None:
        self.supervisor.stop(13.0)
        interrupt_erasure(self.home, TID)
        other = review_supervisor.Supervisor(self.home, self.policy)
        self.addCleanup(other.stop, 20.0)
        other.start(14.0)
        with mock.patch.object(other, "_create") as create:
            other.tick(15.0)
        self.assertEqual(create.call_count, 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM tombstone").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )

    def test_replay_failures_preserve_complete_then_retry_without_boot(
        self,
    ) -> None:
        self.supervisor.stop(13.0)
        before = interrupt_erasure(self.home, TID)
        baseline = self.home / state.BASELINE
        store.write_json_atomic(baseline, {"obsolete": True})
        retained = baseline.read_bytes()
        replay = state.reapply_tombstones
        error = OSError("replay cleanup failed")
        other = review_supervisor.Supervisor(self.home, self.policy)
        self.addCleanup(other.stop, 20.0)

        def after_commit(conn: sqlite3.Connection, home: Path) -> int:
            self.assertFalse(conn.in_transaction)
            with self.assertRaises(BlockingIOError):
                state.acquire(home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(home, wait_s=0),
            ):
                self.fail("writer released")
            replay(conn, home)
            raise error

        for failure in (error, after_commit):
            with (
                mock.patch.object(
                    state, "reapply_tombstones", side_effect=failure
                ),
                mock.patch.object(review_supervisor, "load_prompt") as prompt,
                mock.patch.object(other, "_create") as create,
                self.assertRaises(OSError) as caught,
            ):
                other.start(14.0)
            self.assertIs(caught.exception, error)
            self.assertEqual((prompt.call_count, create.call_count), (0, 0))
            self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
            self.assertEqual(baseline.read_bytes(), retained)
        other.start(15.0)
        self.assertFalse(baseline.exists())
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )

    def test_busy_reconciliation_keeps_active_until_buffers_are_drained(
        self,
    ) -> None:
        self.supervisor.tick(13.0)
        original = state.complete_owner

        def check(home: Path, fd: int, token: str) -> None:
            self.assertFalse(self.supervisor.monitor.slots)
            self.assertFalse(self.supervisor._rounds)
            self.assertFalse(self.supervisor._pending)
            self.assertFalse(self.supervisor._next)
            self.assertEqual(self.supervisor.artifact, b"")
            original(home, fd, token)

        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaises(store.BusyError),
        ):
            self.supervisor.stop(14.0)
        self.assertTrue(self.supervisor._rounds)
        self.assertEqual(
            json.loads((self.home / state.WITNESS).read_bytes())["state"],
            "active",
        )
        with mock.patch.object(state, "complete_owner", side_effect=check):
            self.supervisor.stop(15.0)
        self.assertEqual(
            json.loads((self.home / state.WITNESS).read_bytes())["state"],
            "complete",
        )

    def test_poller_shares_token_and_consumes_stop_on_all_platforms(
        self,
    ) -> None:
        self.supervisor.stop(14.0)
        poller = cli_serve._Poller(self.home, {}, 60)
        self.assertIsNone(poller.stop_generation)
        with mock.patch.object(
            poller_stop,
            "requested",
            side_effect=AssertionError("pre-start lookup"),
        ):
            self.assertFalse(poller._requested())
        poller._start_review()
        self.addCleanup(poller.close)
        assert (
            poller.supervisor is not None
            and poller.stop_generation is not None
        )
        self.assertEqual(
            poller.stop_generation, poller.supervisor.stop_generation
        )
        value = state.parse(
            state.inspect(self.home, state.WITNESS)[0], state.Witness
        )
        assert value is not None and value.owner is not None
        self.assertEqual(value.owner.generation, poller.stop_generation)
        poller_stop.request(self.home, os.getpid(), poller.stop_generation)
        self.assertTrue(poller._requested())
