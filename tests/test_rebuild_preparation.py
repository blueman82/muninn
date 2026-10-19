"""Exercise real preparation gaps beneath retained rebuild ownership."""

from __future__ import annotations

import os
import sqlite3
import unittest
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli_rebuild, ingest, ingest_plan, scope, store
from muninn import review_quiescence as q
from muninn import review_quiescence_state as state
from tests.cli_support import CliCase
from tests.installer_support import World
from tests.review_quiescence_support import initialize
from tests.test_classify import codex_meta, user_msg
from tests.test_ingest import rollout


class RebuildPreparationTests(CliCase):
    """Join real Git preparation to full source and connection fencing."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.world = World(self)
        for name, cwd in (("a", self.world.repo), ("b", self.tmp / "gone")):
            self.write(
                rollout(name),
                [
                    codex_meta(
                        "user",
                        name,
                        cwd=str(cwd),
                        git={"commit_hash": self.world.sha},
                    ),
                    user_msg(1, f"Please retain {name}."),
                ],
            )

    def test_real_git_is_writer_free_with_closed_connections(self) -> None:
        connections: list[sqlite3.Connection] = []
        owners: list[tuple[int, float]] = []
        probes: list[tuple[object, object]] = []
        connect, reader, writer, run = (
            store.connect_rw,
            store.connect_ro,
            q.maintenance_writer,
            scope.subprocess.run,
        )

        def opened(
            path: Path, *, readonly: bool = False
        ) -> sqlite3.Connection:
            with self.assertRaises(BlockingIOError):
                state.acquire(self.home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("SQLite opened without writer")
            conn = reader(path) if readonly else connect(path)
            connections.append(conn)
            return conn

        def region(owner: q.MaintenanceOwner) -> object:
            owners.append((id(owner), owner.deadline))
            return writer(owner)

        def probe(*args: Any, **kwargs: Any) -> Any:
            with self.assertRaises(BlockingIOError):
                state.acquire(self.home)
            with store.writer_lock(self.home, wait_s=0):
                pass
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            probes.append((kwargs.get("close_fds"), kwargs.get("timeout")))
            return run(*args, **kwargs)

        with (
            mock.patch.object(store, "connect_rw", side_effect=opened),
            mock.patch.object(
                store,
                "connect_ro",
                side_effect=lambda path: opened(path, readonly=True),
            ),
            mock.patch.object(q, "maintenance_writer", side_effect=region),
            mock.patch.object(scope.subprocess, "run", side_effect=probe),
        ):
            out = cli_rebuild._run(self.env, self.home, cli_rebuild._Status())
        self.assertEqual(probes, [(True, 10)])
        self.assertEqual(len(owners), 3)
        self.assertEqual(len(set(owners)), 1)
        self.assertEqual(
            (
                out["ingest"]["files_seen"],
                out["ingest"]["files_changed"],
                out["ingest"]["events_added"],
                out["ingest"]["events_removed"],
            ),
            (4, 3, 3, 1),
        )
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        os.close(state.acquire(self.home))

    def test_same_size_mtime_writer_change_refuses_before_next_open(
        self,
    ) -> None:
        prepare = scope.prepare_commit
        changed: list[bytes] = []

        def write(
            lookup: scope.CommitLookup, facts: scope.CommitFacts
        ) -> None:
            prepare(lookup, facts)
            db = store.db_path(self.home)
            before = db.stat()
            with (
                store.writer_lock(self.home, wait_s=0),
                closing(store.connect_rw(db)) as conn,
            ):
                conn.execute(
                    "UPDATE review_control SET generation=generation+1"
                )
            os.utime(db, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(db.stat().st_size, before.st_size)
            changed.append(db.read_bytes())

        with mock.patch.object(scope, "prepare_commit", side_effect=write):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertFalse(out["publication_possible"])
        self.assertEqual(store.db_path(self.home).read_bytes(), changed[0])
        self.assertEqual(len(changed), 1)

    def test_earlier_failed_attempt_never_publishes_after_recovery(
        self,
    ) -> None:
        self.write(
            rollout("0"),
            [
                codex_meta(
                    "user",
                    "0",
                    cwd=str(self.world.repo),
                    git={"commit_hash": self.world.sha},
                ),
                user_msg(1, "Please retain the first source."),
            ],
        )
        parse = ingest.parse_source
        attempts = 0

        def transient(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("source temporarily unavailable")
            return parse(*args, **kwargs)

        with mock.patch.object(ingest, "parse_source", side_effect=transient):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual(
            (code, out["reason"]), (2, "rebuild_ingest_incomplete")
        )
        self.assertFalse(out["publication_possible"])
        self.assertTrue(out["recovery_required"])
        self.assertGreaterEqual(attempts, 4)
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )

    def refuse_gap(self, mutate: Callable[[Path], None]) -> None:
        """Mutate a fresh current fixture while the actual writer is free."""
        home = self.tmp / f"gap-{len(list(self.tmp.glob('gap-*')))}"
        with closing(store.connect_rw(store.db_path(home))):
            pass
        initialize(home)
        prepare = scope.prepare_commit

        def intervening(
            lookup: scope.CommitLookup, facts: scope.CommitFacts
        ) -> None:
            prepare(lookup, facts)
            with store.writer_lock(home, wait_s=0):
                mutate(home)

        with (
            mock.patch.object(
                scope, "prepare_commit", side_effect=intervening
            ),
            mock.patch.object(
                cli_rebuild.platform_rebuild, "publish_replacement"
            ) as publish,
        ):
            code, out, _ = self.muninn(
                "rebuild", env={"MUNINN_HOME": str(home)}
            )
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertTrue(out["recovery_required"])
        self.assertFalse(out["publication_possible"])
        publish.assert_not_called()
        self.assertTrue((home / cli_rebuild.REBUILD).exists())

    @unittest.skipIf(os.name == "nt", "POSIX fchmod ctime behavior")
    def test_ctime_only_change_across_gap_still_refuses(self) -> None:
        def change(home: Path) -> None:
            path = store.db_path(home)
            before = path.stat()
            path.chmod(0o600)
            after = path.stat()
            self.assertEqual(
                (after.st_ino, after.st_size, after.st_mtime_ns),
                (before.st_ino, before.st_size, before.st_mtime_ns),
            )
            self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)

        self.refuse_gap(change)

    def test_same_bytes_new_inode_across_gap_refuses(self) -> None:
        def change(home: Path) -> None:
            path = store.db_path(home)
            raw = path.read_bytes()
            path.rename(home / "original.sqlite")
            path.write_bytes(raw)
            path.chmod(0o600)

        self.refuse_gap(change)

    def test_key_intent_and_every_sidecar_change_refuse(self) -> None:
        for name in (
            "tombstone.key",
            "tombstones.jsonl",
            "muninn.sqlite-journal",
            "muninn.sqlite-wal",
            "muninn.sqlite-shm",
        ):
            with self.subTest(name=name):

                def change(home: Path, name: str = name) -> None:
                    path = home / name
                    path.write_bytes(b"new private state")
                    path.chmod(0o600)

                self.refuse_gap(change)

    def test_candidate_and_archive_changes_refuse(self) -> None:
        for candidate in (True, False):
            with self.subTest(candidate=candidate):

                def change(home: Path, candidate: bool = candidate) -> None:
                    path = (
                        home / cli_rebuild.REBUILD
                        if candidate
                        else next(home.glob(store.RECOVERY_PREFIX + "*"))
                    )
                    with path.open("r+b") as handle:
                        handle.seek(-1, os.SEEK_END)
                        handle.write(b"X")

                self.refuse_gap(change)

    def test_original_deadline_bounds_reacquisition_after_git(self) -> None:
        prepare, writer = scope.prepare_commit, q.maintenance_writer
        owners: list[q.MaintenanceOwner] = []
        with ExitStack() as resources:

            def region(owner: q.MaintenanceOwner) -> object:
                owners.append(owner)
                return writer(owner)

            def contend(
                lookup: scope.CommitLookup, facts: scope.CommitFacts
            ) -> None:
                prepare(lookup, facts)
                resources.enter_context(store.writer_lock(self.home, wait_s=0))
                resources.enter_context(
                    mock.patch.object(
                        q.time,
                        "monotonic",
                        return_value=owners[0].deadline + 1,
                    )
                )

            with (
                mock.patch.object(q, "maintenance_writer", side_effect=region),
                mock.patch.object(
                    scope, "prepare_commit", side_effect=contend
                ),
                mock.patch.object(q.time, "sleep") as sleep,
            ):
                code, out, _ = self.muninn("rebuild")
            self.assertEqual((code, out["error"]), (3, "busy"))
            self.assertFalse(out["publication_possible"])
            self.assertTrue(out["recovery_required"])
            self.assertEqual(len({id(owner) for owner in owners}), 1)
            sleep.assert_not_called()

    def test_unreadable_source_refuses_despite_valid_candidate_database(
        self,
    ) -> None:
        with mock.patch.object(
            ingest_plan, "open_regular", side_effect=PermissionError
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual(
            (code, out["reason"]), (2, "rebuild_ingest_incomplete")
        )
        self.assertFalse(out["publication_possible"])
        with closing(
            store.connect_ro(self.home / cli_rebuild.REBUILD)
        ) as conn:
            self.assertEqual(
                conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
