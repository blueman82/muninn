"""Exercise fresh transcript work with real Git and maintenance barriers."""

from __future__ import annotations

import os
import sqlite3
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from install import cutover, steps_release
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import cursor_import, ingest, ingest_plan, scope, store
from tests import test_cursor_precompact
from tests.ingest_support import line
from tests.installer_support import World
from tests.test_classify import codex_meta, reply, user_msg


class FreshIngestTests(unittest.TestCase):
    """Keep the owner across full replans without holding material handles."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        self.ctx.rdir.mkdir(parents=True)
        self.record: Record = {"sha": self.world.sha}
        self.path = cutover.prepare_store(self.ctx, self.record)
        self.gone = self.world.home / "deleted-project"
        self.source("a", self.world.repo)
        self.source("b", self.gone)
        env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.world.home),
                "USERPROFILE": str(self.world.home),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def source(self, name: str, cwd: Path) -> Path:
        """Write an isolated physical source carrying a real commit hint."""
        path = self.ctx.codex_home / "sessions" / f"rollout-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            codex_meta(
                "user", name, cwd=str(cwd), git={"commit_hash": self.world.sha}
            ),
            user_msg(1, name),
            reply(2, name + " answer"),
        ]
        path.write_bytes(b"".join(map(line, records)))
        return path

    def test_real_git_replans_with_one_owner_and_closed_connections(
        self,
    ) -> None:
        connect, writer, run = (
            store.connect_rw,
            cutover.writer,
            scope.subprocess.run,
        )
        connections: list[sqlite3.Connection] = []
        regions: list[int] = []
        probes: list[tuple[object, object, list[str]]] = []

        @contextmanager
        def region(ctx: Ctx) -> Generator[None]:
            regions.append(id(ctx))
            with writer(ctx):
                try:
                    yield
                finally:
                    for conn in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            conn.execute("SELECT 1")

        def opened(path: Path) -> sqlite3.Connection:
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.ctx.data, wait_s=0),
            ):
                self.fail("writer free during SQLite open")
            conn = connect(path)
            connections.append(conn)
            return conn

        def probe(*args: Any, **kwargs: Any) -> Any:
            with store.writer_lock(self.ctx.data, wait_s=0):
                pass
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            probes.append(
                (kwargs.get("close_fds"), kwargs.get("timeout"), args[0])
            )
            return run(*args, **kwargs)

        with (
            mock.patch.object(store, "connect_rw", side_effect=opened),
            mock.patch.object(cutover, "writer", side_effect=region),
            mock.patch.object(scope.subprocess, "run", side_effect=probe),
        ):
            stats = cutover.ingest_transcripts(self.ctx)
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][:2], (True, 10))
        self.assertEqual(
            probes[0][2][1:4], ["-C", str(self.world.repo), "cat-file"]
        )
        self.assertEqual(len(connections), 2)
        self.assertEqual(len(regions), 2)
        self.assertEqual(len(set(regions)), 1)
        self.assertEqual(
            (
                stats.files_seen,
                stats.files_changed,
                stats.events_added,
                stats.events_removed,
                stats.failed,
                stats.errors,
            ),
            (4, 3, 6, 2, 0, {}),
        )
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 4
            )
            key = conn.execute(
                "SELECT s.key FROM scope s"
                " JOIN scope_path p ON p.scope_id=s.id"
                " WHERE p.cwd=?",
                (str(self.gone),),
            ).fetchone()[0]
            self.assertEqual(key, str(self.world.repo))

    def test_replaced_store_during_probe_refuses_before_next_open(
        self,
    ) -> None:
        prepare = scope.prepare_commit
        connect = store.connect_rw
        connections: list[sqlite3.Connection] = []

        def opened(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            connections.append(conn)
            return conn

        def replace(
            lookup: scope.CommitLookup, facts: scope.CommitFacts
        ) -> None:
            prepare(lookup, facts)
            self.path.rename(self.path.with_name("retained.sqlite"))
            with closing(connect(self.path)):
                pass

        with (
            mock.patch.object(scope, "prepare_commit", side_effect=replace),
            mock.patch.object(store, "connect_rw", side_effect=opened),
            self.assertRaisesRegex(OSError, "changed identity"),
        ):
            cutover.ingest_transcripts(self.ctx)
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_accumulated_failure_refuses_even_after_source_recovers(
        self,
    ) -> None:
        self.source("0-fails", self.world.repo)
        parse = ingest.parse_source
        attempts = 0

        def transient(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic source unavailable")
            return parse(*args, **kwargs)

        with (
            mock.patch.object(ingest, "parse_source", side_effect=transient),
            self.assertRaisesRegex(StepFailedError, "fresh_ingest_incomplete"),
        ):
            cutover.ingest_transcripts(self.ctx)
        self.assertGreaterEqual(attempts, 5)
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 6
            )

    def test_unreadable_visit_refuses_incomplete_result(self) -> None:
        with (
            mock.patch.object(
                ingest_plan, "open_regular", side_effect=PermissionError
            ),
            self.assertRaisesRegex(StepFailedError, "fresh_ingest_incomplete"),
        ):
            cutover.ingest_transcripts(self.ctx)

    @unittest.skipIf(
        os.name == "nt", "Windows prevents replacing an open SQLite file"
    )
    def test_replacement_during_open_refuses_before_material_access(
        self,
    ) -> None:
        connect = store.connect_rw
        connections: list[sqlite3.Connection] = []

        def opened(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            connections.append(conn)
            path.rename(path.with_name("retained.sqlite"))
            with closing(connect(path)):
                pass
            return conn

        with (
            mock.patch.object(store, "connect_rw", side_effect=opened),
            mock.patch.object(ingest, "ingest") as material,
            self.assertRaisesRegex(OSError, "changed identity"),
        ):
            cutover.ingest_transcripts(self.ctx)
        material.assert_not_called()
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")


class PreparedFreshTests(unittest.TestCase):
    """Complete real provider work before exposing the prepared store."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        self.ctx.rdir.mkdir(parents=True)
        self.record: Record = {"sha": self.world.sha}
        self.cursor = test_cursor_precompact.CursorPrecompactTests()
        self.addCleanup(self.cursor.doCleanups)
        self.cursor.setUp()
        self.cursor.conn.close()
        self.source = self.ctx.codex_home / "sessions/rollout-fresh.jsonl"
        self.source.parent.mkdir(parents=True)
        records = [
            codex_meta(
                "user",
                "fresh",
                cwd=str(self.world.repo),
                git={"commit_hash": self.world.sha},
            ),
            user_msg(1, "fresh prompt"),
            reply(2, "fresh answer"),
        ]
        self.source.write_bytes(b"".join(map(line, records)))
        gone = self.source.with_name("rollout-z-gone.jsonl")
        records[0] = codex_meta(
            "user",
            "gone",
            cwd=str(self.world.home / "gone"),
            git={"commit_hash": self.world.sha},
        )
        gone.write_bytes(b"".join(map(line, records)))

    def test_prepared_store_finishes_both_providers_before_yield(self) -> None:
        connect, prepare, apply, writer = (
            store.connect_rw,
            cursor_import.prepare,
            cursor_import.apply,
            cutover.writer,
        )
        handles: list[sqlite3.Connection] = []
        owners: list[int] = []
        original = self.source.read_bytes(), self.cursor.database.read_bytes()

        @contextmanager
        def region(ctx: Ctx) -> Generator[None]:
            owners.append(id(ctx))
            with writer(ctx):
                try:
                    yield
                finally:
                    for conn in handles:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            conn.execute("SELECT 1")

        def opened(path: Path) -> sqlite3.Connection:
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.ctx.data, wait_s=0),
            ):
                self.fail("writer free during material open")
            conn = connect(path)
            handles.append(conn)
            return conn

        def prepared(
            *args: Any, **kwargs: Any
        ) -> cursor_import.PreparedImport:
            with store.writer_lock(self.ctx.data, wait_s=0):
                pass
            for conn in handles:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            return prepare(*args, **kwargs)

        with (
            mock.patch.object(
                cursor_import,
                "default_database",
                return_value=self.cursor.database,
            ),
            mock.patch.object(
                cursor_import, "prepare", side_effect=prepared
            ) as prep,
            mock.patch.object(cursor_import, "apply", wraps=apply) as applied,
            mock.patch.object(store, "connect_rw", side_effect=opened),
            mock.patch.object(cutover, "writer", side_effect=region),
            cutover.prepared_store(self.ctx, self.record) as path,
        ):
            with closing(store.connect_ro(path)) as conn:
                actual = dict(
                    conn.execute(
                        "SELECT s.provider,count(*) FROM event e"
                        " JOIN source s ON s.id=e.source_id"
                        " GROUP BY s.provider"
                    )
                )
                self.assertEqual(actual, {"codex": 4, "cursor": 2})
                self.assertEqual(
                    tuple(
                        conn.execute(
                            "SELECT admission,policy_json FROM review_control"
                        ).fetchone()
                    ),
                    (0, None),
                )
            prep.assert_called_once()
            applied.assert_called_once()
            self.assertEqual(len(owners), 4)
            self.assertEqual(len(set(owners)), 1)
            self.assertEqual(self.record["events_by_provider"], actual)
            self.assertEqual(self.record["ingest"]["events_added"], 6)
            self.assertTrue((self.ctx.data / "status.json").exists())
            with store.writer_lock(self.ctx.data, wait_s=0):
                pass
            with mock.patch.object(steps_release, "must") as child:
                steps_release.ingest_fresh(self.ctx, self.record)
            child.assert_not_called()
        self.assertEqual(
            original,
            (self.source.read_bytes(), self.cursor.database.read_bytes()),
        )
        self.assertEqual(len(handles), 4)
        self.assertTrue((self.ctx.data / "recall.off").exists())
