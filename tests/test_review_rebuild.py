"""Exercise controlled initialization and maintenance in temp homes."""

from __future__ import annotations

import contextlib
import sqlite3
import unittest
from collections.abc import Generator
from pathlib import Path
from unittest import mock

from install import current_store, cutover
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import store
from muninn.review_models import ReviewContext
from tests.home_support import initialize, interrupt_erasure
from tests.installer_support import World
from tests.policy_patch import reserve_call
from tests.test_review_commit import CommitCase


class FreshStoreTests(unittest.TestCase):
    """Require positive new-home creation before seeding containment."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        self.ctx.rdir.mkdir(parents=True)
        self.record: Record = {"sha": self.world.sha}

    def test_no_policy_initializes_without_admitting_learning(self) -> None:
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("spawn")
        ):
            path = cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(path, store.db_path(self.ctx.data))
        with contextlib.closing(store.connect_ro(path)) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM review_control").fetchone()[:],
                (1, 0, 0, None, None, None),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
        self.assertEqual(self.world.fake.calls, [])

    def test_preexisting_empty_home_is_not_fresh(self) -> None:
        self.ctx.data.mkdir(mode=0o700)
        with self.assertRaisesRegex(StepFailedError, "fresh_home_exists"):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(list(self.ctx.data.iterdir()), [])

    def test_replaced_home_refuses_before_store(self) -> None:
        acquire = cutover.writer

        def replace(ctx: object) -> contextlib.AbstractContextManager[None]:
            home = self.ctx.data
            home.rename(home.with_name("original-home"))
            home.mkdir(mode=0o700)
            return acquire(self.ctx)

        with (
            mock.patch.object(cutover, "writer", side_effect=replace),
            self.assertRaisesRegex(StepFailedError, "fresh_home_changed"),
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertFalse(store.db_path(self.ctx.data).exists())

    def test_connection_opens_under_the_writer_lock_and_closes_before_release(
        self,
    ) -> None:
        connect = store.connect_rw
        writer = cutover.writer
        connections: list[sqlite3.Connection] = []
        regions: list[int] = []

        @contextlib.contextmanager
        def region(ctx: Ctx) -> Generator[None]:
            regions.append(id(ctx))
            with writer(ctx):
                try:
                    yield
                finally:
                    for conn in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            conn.execute("SELECT 1")

        def open_checked(path: Path) -> sqlite3.Connection:
            for previous in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    previous.execute("SELECT 1")
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(path.parent, wait_s=0),
            ):
                self.fail("writer was not held")
            conn = connect(path)
            connections.append(conn)
            return conn

        with (
            mock.patch.object(store, "connect_rw", side_effect=open_checked),
            mock.patch.object(cutover, "writer", side_effect=region),
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(len(connections), 3)
        self.assertEqual(len({id(conn) for conn in connections}), 3)
        self.assertEqual(len(regions), 3)
        self.assertEqual(len(set(regions)), 1)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        with store.writer_lock(self.ctx.data, wait_s=0):
            pass


class CurrentStoreTests(CommitCase):
    """Keep current data in place while fencing abandoned review work."""

    def setUp(self) -> None:
        super().setUp()
        self.world = World(self)
        self.ctx = self.world.ctx(upgrade=True)
        self.ctx.data = self.home
        self.ctx.rdir.mkdir(parents=True)
        self.record: Record = {"sha": self.world.sha}

    def ready(self) -> None:
        """Close fixture handles before proving this owned home's creation."""
        self.conn.close()
        initialize(self.home)

    def reopen(self) -> None:
        """Open a new inspection handle after maintenance has released."""
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)

    def test_current_path_preserves_policy_and_unknown_charges(self) -> None:
        request = self.row("SELECT * FROM review_request")
        context = ReviewContext(
            request_id=self.context.request_id,
            attempt_id=self.context.attempt_id,
            generation=1,
            scope=self.scope,
            policy=self.policy,
            admitted_windows=(self.window(),),
            evidence_ids=(),
            round_index=1,
            deadline=72.0,
        )
        call = self.transaction(lambda: reserve_call(self.conn, context, 13.0))
        control = tuple(self.row("SELECT * FROM review_control"))
        db = store.db_path(self.home)
        identity = db.stat().st_dev, db.stat().st_ino
        self.ready()
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("spawn")
        ):
            self.assertEqual(cutover.prepare_store(self.ctx, self.record), db)
        self.reopen()
        self.assertEqual((db.stat().st_dev, db.stat().st_ino), identity)
        after = tuple(self.row("SELECT * FROM review_control"))
        self.assertEqual(after, control)
        recovered = self.row("SELECT * FROM review_request")
        self.assertEqual(recovered["id"], request["id"])
        self.assertEqual(recovered["state"], "waiting")
        self.assertIsNone(recovered["current_attempt_id"])
        charged = self.row(
            "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
            (call.attempt_id, call.round_index),
        )
        self.assertEqual(charged["state"], "unknown")
        self.assertIsNone(charged["usage_json"])
        self.assertEqual(self.record["store"]["phase"], "current_prepared")
        self.assertEqual(self.world.fake.calls, [])

    def test_no_policy_does_not_gain_admission(self) -> None:
        self.conn.execute(
            "UPDATE review_control SET admission=0,policy_json=NULL,"
            "policy_digest=NULL,activated_at=NULL"
        )
        self.ready()
        cutover.prepare_store(self.ctx, self.record)
        self.reopen()
        self.assertEqual(
            tuple(self.row("SELECT * FROM review_control")),
            (1, 1, 0, None, None, None),
        )

    def test_pending_intent_replays_before_permitted_admission(self) -> None:
        thread = self.row("SELECT thread_id FROM source")[0]
        self.save(self.result([self.note()]))
        self.ready()
        interrupt_erasure(self.home, thread)
        cutover.prepare_store(self.ctx, self.record)
        self.reopen()
        self.assertEqual(self.row("SELECT count(*) FROM tombstone")[0], 1)
        self.assertIsNone(
            self.row("SELECT content_json FROM knowledge_version")[0]
        )
        self.assertEqual(self.row("SELECT count(*) FROM knowledge_fts")[0], 0)
        self.assertEqual(
            self.row("SELECT admission FROM review_control")[0], 1
        )

    def test_replaced_source_rejected_before_fencing_stale_connection(
        self,
    ) -> None:
        self.ready()
        connect = store.connect_rw
        db = store.db_path(self.home)
        old = db.with_name("held.sqlite3")
        opened: list[sqlite3.Connection] = []

        def replace(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            opened.append(conn)
            path.rename(old)
            with contextlib.closing(connect(path)):
                pass
            return conn

        with (
            mock.patch.object(store, "connect_rw", side_effect=replace),
            self.assertRaisesRegex(OSError, "changed identity"),
        ):
            cutover.prepare_store(self.ctx, self.record)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        with contextlib.closing(store.connect_ro(old)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                1,
            )
        with contextlib.closing(store.connect_ro(db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                0,
            )

    def test_record_failure_prevents_live_fencing(self) -> None:
        self.ready()
        failure = OSError("record not durable")
        with (
            mock.patch.object(cutover.record, "save", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertIs(caught.exception, failure)
        self.reopen()
        self.assertEqual(
            self.row("SELECT generation FROM review_control")[0], 1
        )

    def test_replaced_path_does_not_mask_original_replay_error(self) -> None:
        self.ready()
        failure = LookupError("replay interrupted")
        db = store.db_path(self.home)
        handles: list[sqlite3.Connection] = []

        def fail(conn: sqlite3.Connection, home: Path) -> int:
            handles.append(conn)
            db.rename(db.with_name("held.sqlite3"))
            with contextlib.closing(store.connect_rw(db)):
                pass
            raise failure

        with (
            mock.patch.object(
                current_store, "reapply_tombstones", side_effect=fail
            ),
            self.assertRaises(LookupError) as caught,
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertIs(caught.exception, failure)
        with self.assertRaises(sqlite3.ProgrammingError):
            handles[0].execute("SELECT 1")
        with store.writer_lock(self.home, wait_s=0):
            pass
