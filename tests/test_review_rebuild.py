"""Exercise controlled initialization and maintenance in temp homes."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import unittest
from collections.abc import Generator
from pathlib import Path
from unittest import mock

from install import cutover
from install.context import StepFailedError
from install.record import Record
from muninn import review_budget, store
from muninn import review_pause as q
from muninn import review_pause_state as state
from muninn.review_models import ReviewContext
from tests.installer_support import World
from tests.review_pause_support import initialize, interrupt_erasure
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
        with (
            q.maintenance_owner(
                self.ctx.data, wait_s=0, status=q.EraseStatus()
            ) as owner,
            q.maintenance_writer(owner),
            contextlib.closing(store.connect_rw(path)) as conn,
        ):
            q.verify_current(owner, conn)
        self.assertEqual(self.world.fake.calls, [])

    def test_preexisting_empty_home_is_not_fresh(self) -> None:
        self.ctx.data.mkdir(mode=0o700)
        with self.assertRaisesRegex(StepFailedError, "fresh_home_exists"):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(list(self.ctx.data.iterdir()), [])

    def test_replaced_home_refuses_before_store_or_witness(self) -> None:
        acquire = q.maintenance_owner

        def replace(
            home: Path, **kwargs: object
        ) -> contextlib.AbstractContextManager[q.MaintenanceOwner]:
            home.rename(home.with_name("original-home"))
            home.mkdir(mode=0o700)
            return acquire(home, wait_s=0, status=q.EraseStatus())

        with (
            mock.patch.object(q, "maintenance_owner", side_effect=replace),
            self.assertRaisesRegex(StepFailedError, "fresh_home_changed"),
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertFalse(store.db_path(self.ctx.data).exists())
        self.assertFalse((self.ctx.data / state.WITNESS).exists())

    def test_new_witness_or_baseline_prevents_initialization(self) -> None:
        writer = q.maintenance_writer
        for name in (state.WITNESS, state.BASELINE):
            with self.subTest(name=name):
                ctx = self.world.ctx(fresh=True)
                ctx.data = self.ctx.data.with_name(
                    name.replace(".", "-") + "-home"
                )

                def plant(
                    owner: q.MaintenanceOwner,
                    name: str = name,
                ) -> contextlib.AbstractContextManager[None]:
                    path = owner.home / name
                    path.write_text("{}")
                    path.chmod(0o600)
                    return writer(owner)

                with (
                    mock.patch.object(
                        q, "maintenance_writer", side_effect=plant
                    ),
                    self.assertRaisesRegex(ValueError, "pause_unverified"),
                ):
                    cutover.prepare_store(ctx, self.record)
                self.assertFalse(store.db_path(ctx.data).exists())

    def test_connection_opens_under_both_barriers_and_closes_before_release(
        self,
    ) -> None:
        connect = store.connect_rw
        writer = q.maintenance_writer
        connections: list[sqlite3.Connection] = []
        regions: list[tuple[int, float]] = []

        @contextlib.contextmanager
        def region(owner: q.MaintenanceOwner) -> Generator[None]:
            regions.append((id(owner), owner.deadline))
            with writer(owner):
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
            with self.assertRaises(BlockingIOError):
                state.acquire(path.parent)
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
            mock.patch.object(q, "maintenance_writer", side_effect=region),
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertEqual(len(connections), 3)
        self.assertEqual(len({id(conn) for conn in connections}), 3)
        self.assertEqual(len(regions), 3)
        self.assertEqual(len(set(regions)), 1)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        os.close(state.acquire(self.ctx.data))
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
        call = self.transaction(
            lambda: review_budget.reserve_call(self.conn, context, 13.0)
        )
        control = tuple(self.row("SELECT * FROM review_control"))
        db = store.db_path(self.home)
        identity = db.stat().st_dev, db.stat().st_ino
        self.ready()
        witness = (self.home / state.WITNESS).read_bytes()
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("spawn")
        ):
            self.assertEqual(cutover.prepare_store(self.ctx, self.record), db)
        self.reopen()
        self.assertEqual((db.stat().st_dev, db.stat().st_ino), identity)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), witness)
        after = tuple(self.row("SELECT * FROM review_control"))
        self.assertEqual(after, (1, 2, 1, *control[3:]))
        recovered = self.row("SELECT * FROM review_request")
        self.assertEqual(recovered["id"], request["id"])
        self.assertEqual(recovered["state"], "waiting")
        self.assertIsNone(recovered["current_attempt_id"])
        charged = self.row(
            "SELECT * FROM review_call WHERE attempt_id=? AND round_index=?",
            (call.attempt_id, call.round_index),
        )
        self.assertEqual(charged["state"], "unknown")
        self.assertEqual(
            charged["cost_reservation_decimal"], call.cost_reservation_decimal
        )
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
            (1, 2, 0, None, None, None),
        )

    def test_invalid_policy_leaves_admission_disabled_and_attempt_fenced(
        self,
    ) -> None:
        self.conn.execute("UPDATE review_control SET policy_digest='mismatch'")
        self.ready()
        with self.assertRaisesRegex(ValueError, "review_policy_required"):
            cutover.prepare_store(self.ctx, self.record)
        self.reopen()
        self.assertEqual(
            tuple(self.row("SELECT generation,admission FROM review_control")),
            (2, 0),
        )
        self.assertIsNotNone(
            self.row("SELECT ended_at FROM review_attempt")[0]
        )
        self.assertEqual(
            self.record["store"]["phase"], "current_mutation_possible"
        )

    def test_unverified_home_never_seeds_itself(self) -> None:
        self.conn.close()
        with self.assertRaisesRegex(ValueError, "pause_unverified"):
            cutover.prepare_store(self.ctx, self.record)
        self.assertFalse((self.home / state.WITNESS).exists())
        self.reopen()
        self.assertEqual(
            self.row("SELECT generation FROM review_control")[0], 1
        )

    def test_pending_intent_replays_before_permitted_admission(self) -> None:
        thread = self.row("SELECT thread_id FROM source")[0]
        self.save(self.result([self.note()]))
        self.ready()
        witness = interrupt_erasure(self.home, thread)
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
        self.assertEqual((self.home / state.WITNESS).read_bytes(), witness)

    @unittest.skipIf(os.name == "nt", "Windows locks open SQLite stores")
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

    @unittest.skipIf(os.name == "nt", "Windows locks open SQLite stores")
    def test_replaced_path_does_not_mask_original_replay_error(self) -> None:
        self.ready()
        failure = LookupError("replay interrupted")
        db = store.db_path(self.home)
        handles: list[sqlite3.Connection] = []

        def fail(owner: q.MaintenanceOwner, conn: sqlite3.Connection) -> None:
            handles.append(conn)
            db.rename(db.with_name("held.sqlite3"))
            with contextlib.closing(store.connect_rw(db)):
                pass
            raise failure

        with (
            mock.patch.object(q, "verify_current", side_effect=fail),
            self.assertRaises(LookupError) as caught,
        ):
            cutover.prepare_store(self.ctx, self.record)
        self.assertIs(caught.exception, failure)
        with self.assertRaises(sqlite3.ProgrammingError):
            handles[0].execute("SELECT 1")
        os.close(state.acquire(self.home))
        with store.writer_lock(self.home, wait_s=0):
            pass
