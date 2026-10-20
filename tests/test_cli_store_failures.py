"""Preserve selective SQLite refusals at normalized CLI boundaries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli_know, cli_learn, store
from muninn.review_models import ProposalInput
from tests.test_cli_learning import CliLearningCase


def sqlite_failure(code: int | None) -> sqlite3.OperationalError:
    """Build an injected boundary failure without exposing stored values."""
    error = sqlite3.OperationalError("PRIVATE-SQLITE-VALUE")
    if code is not None:
        error.sqlite_errorcode = code
        error.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
    return error


def track_readers(
    connections: list[sqlite3.Connection],
) -> Callable[[Path], sqlite3.Connection]:
    """Record real reader connections without replacing their cleanup."""
    connect = store.connect_ro

    def reader(path: Path) -> sqlite3.Connection:
        conn = connect(path)
        connections.append(conn)
        return conn

    return reader


class CliStoreFailureTests(CliLearningCase):
    """Exercise physical corruption, cleanup and narrowly injected codes."""

    def action(self, name: str) -> tuple[int, dict[str, Any]]:
        """Invoke one normalized reader or writer with valid arguments."""
        if name == "propose":
            return self.propose()
        if name == "retract":
            return self.call(
                "know",
                "retract",
                "K_" + "0" * 32,
                "--expected-revision",
                "1",
                "--reason",
                "PRIVATE-REASON",
            )
        if name == "cancel":
            return self.call("learn", "cancel", self.context.request_id)
        return self.call(name, "list")

    def assert_closed(self, connections: list[sqlite3.Connection]) -> None:
        """Require every real connection to close before refusal returns."""
        self.assertTrue(connections)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        with store.writer_lock(self.home, wait_s=0):
            pass

    def assert_unavailable(self, code: int, out: dict[str, Any]) -> None:
        """Require sanitized JSON and an actual content-free call record."""
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertIn(
            out["detail"],
            (
                "cannot read the store (SQLITE_BUSY_SNAPSHOT)",
                "cannot read the store (SQLITE_NOTADB)",
            ),
        )
        logs = (self.home / "calls.jsonl").read_text()
        record = json.loads(logs.splitlines()[-1])
        self.assertEqual(
            (record["exit"], record["error"]), (4, "store_unavailable")
        )
        for private in (
            "PRIVATE-SQLITE-VALUE",
            "PRIVATE-REASON",
            "Use Python",
        ):
            self.assertNotIn(private, json.dumps(out) + logs)
        self.assertNotIn(str(self.tmp), json.dumps(out) + logs)

    def corrupt(self, name: str, *, after_open: bool) -> None:
        """Overwrite only a disposable marked store before its next query."""
        self.conn.close()
        db = store.db_path(self.home)
        connections: list[sqlite3.Connection] = []
        connect = sqlite3.connect
        reader, writer = store.connect_ro, store.connect_rw

        def capture(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = connect(*args, **kwargs)
            connections.append(conn)
            return conn

        def open_reader(path: Path) -> sqlite3.Connection:
            conn = reader(path)
            db.write_bytes(b"PRIVATE-SQLITE-VALUE" * 4096)
            return conn

        def open_writer(path: Path) -> sqlite3.Connection:
            conn = writer(path)
            db.write_bytes(b"PRIVATE-SQLITE-VALUE" * 4096)
            return conn

        if not after_open:
            db.write_bytes(b"PRIVATE-SQLITE-VALUE" * 4096)
        with mock.patch.object(sqlite3, "connect", capture):
            if after_open:
                with (
                    mock.patch.object(store, "connect_ro", open_reader),
                    mock.patch.object(store, "connect_rw", open_writer),
                ):
                    code, out = self.action(name)
            else:
                code, out = self.action(name)
        self.assert_unavailable(code, out)
        self.assert_closed(connections)
        self.assertEqual(len(connections), 1)

    def check_corruption(self, *, after_open: bool) -> None:
        """Give every destructive subcase a separate normalized fixture."""
        for name in ("know", "propose", "retract", "learn", "cancel"):
            with self.subTest(command=name, after_open=after_open):
                case = CliStoreFailureTests()
                case.setUp()
                try:
                    case.corrupt(name, after_open=after_open)
                finally:
                    case.doCleanups()

    def test_corrupt_store_at_open_returns_exit_four(self) -> None:
        self.check_corruption(after_open=False)

    def test_corrupt_store_after_open_returns_exit_four(self) -> None:
        self.check_corruption(after_open=True)

    def test_readers_heal_once_and_close_both_connections(self) -> None:
        for module, target, argv, result in (
            (cli_know, "_views", ("know", "list"), {"knowledge": []}),
            (
                cli_learn,
                "_detail",
                ("learn", "show", self.context.request_id),
                {"request": None},
            ),
        ):
            with self.subTest(command=argv):
                connections: list[sqlite3.Connection] = []

                with (
                    mock.patch.object(
                        store, "connect_ro", track_readers(connections)
                    ),
                    mock.patch.object(
                        store, "heal_hot_journal", return_value=True
                    ) as heal,
                    mock.patch.object(
                        module,
                        target,
                        side_effect=[
                            sqlite_failure(sqlite3.SQLITE_READONLY_ROLLBACK),
                            result,
                        ],
                    ) as work,
                ):
                    self.assertEqual(self.call(*argv)[0], 0)
                self.assertEqual(work.call_count, 2)
                heal.assert_called_once_with(
                    store.db_path(self.home), self.home
                )
                self.assertEqual(len(connections), 2)
                self.assert_closed(connections)

    def test_second_hot_journal_refuses_without_another_heal(self) -> None:
        for module, target, argv in (
            (cli_know, "_views", ("know", "list")),
            (cli_learn, "_detail", ("learn", "show", self.context.request_id)),
        ):
            with (
                self.subTest(command=argv),
                mock.patch.object(
                    store, "heal_hot_journal", return_value=True
                ) as heal,
                mock.patch.object(
                    module,
                    target,
                    side_effect=sqlite_failure(
                        sqlite3.SQLITE_READONLY_ROLLBACK
                    ),
                ) as work,
            ):
                code, out = self.call(*argv)
                self.assertEqual((code, out["error"]), (4, "hot_journal"))
                self.assertEqual(work.call_count, 2)
                self.assertEqual(heal.call_count, 1)

    def test_extended_store_codes_are_mapped_at_each_boundary(self) -> None:
        for code in (
            sqlite3.SQLITE_BUSY_SNAPSHOT,
            sqlite3.SQLITE_LOCKED_SHAREDCACHE,
            sqlite3.SQLITE_READONLY_DBMOVED,
            sqlite3.SQLITE_IOERR_READ,
            sqlite3.SQLITE_CORRUPT_VTAB,
            sqlite3.SQLITE_CANTOPEN_ISDIR,
            sqlite3.SQLITE_NOTADB,
        ):
            for module, target, name in self.boundaries():
                with (
                    self.subTest(code=code, command=name),
                    mock.patch.object(
                        module, target, side_effect=sqlite_failure(code)
                    ) as work,
                    mock.patch.object(store, "heal_hot_journal") as heal,
                ):
                    self.assert_unavailable(*self.action(name))
                    self.assertEqual(work.call_count, 1)
                    heal.assert_not_called()
                with store.writer_lock(self.home, wait_s=0):
                    pass

    def boundaries(self) -> tuple[tuple[Any, str, str], ...]:
        """Name downstream operations below the normalized public guards."""
        return (
            (cli_know, "_views", "know"),
            (cli_learn, "request_view", "learn"),
            (cli_learn, "_claim_proposal", "propose"),
            (cli_know.knowledge_write, "retract_knowledge", "retract"),
            (cli_learn, "_write", "cancel"),
        )

    def test_unknown_errors_and_interrupts_keep_their_identity(self) -> None:
        for error in (
            sqlite_failure(None),
            sqlite_failure(sqlite3.SQLITE_ERROR),
            sqlite3.ProgrammingError("PRIVATE-SQLITE-VALUE"),
            sqlite3.IntegrityError("PRIVATE-SQLITE-VALUE"),
            sqlite_failure(254),
            sqlite_failure(sqlite3.SQLITE_CONSTRAINT_UNIQUE),
            sqlite_failure(sqlite3.SQLITE_INTERRUPT),
            KeyboardInterrupt(),
            SystemExit(17),
        ):
            for module, target, name in self.boundaries():
                with (
                    self.subTest(error=type(error).__name__, command=name),
                    mock.patch.object(
                        module, target, side_effect=error
                    ) as work,
                    mock.patch.object(store, "heal_hot_journal") as heal,
                    self.assertRaises(type(error)) as caught,
                ):
                    self.action(name)
                self.assertIs(caught.exception, error)
                self.assertEqual(work.call_count, 1)
                heal.assert_not_called()
                with store.writer_lock(self.home, wait_s=0):
                    pass

    def test_direct_proposal_guard_preserves_cleanup_and_never_retries(
        self,
    ) -> None:
        proposal = ProposalInput(
            request_id=self.context.request_id, result=self.result([])
        )
        for code in (
            sqlite3.SQLITE_IOERR_WRITE,
            sqlite3.SQLITE_READONLY_ROLLBACK,
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(
                    cli_learn,
                    "_claim_proposal",
                    side_effect=sqlite_failure(code),
                ) as claim,
                mock.patch.object(store, "heal_hot_journal") as heal,
                self.assertRaises(store.StoreUnavailableError),
            ):
                cli_learn.propose(self.home, proposal)
            self.assertEqual(claim.call_count, 1)
            heal.assert_not_called()
            with store.writer_lock(self.home, wait_s=0):
                pass

    def test_proposal_commit_failure_closes_both_roots_without_retry(
        self,
    ) -> None:
        connections: list[sqlite3.Connection] = []
        connect = store.connect_rw

        def writer(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            connections.append(conn)
            return conn

        with (
            mock.patch.object(store, "connect_rw", writer),
            mock.patch.object(
                cli_learn,
                "commit_review",
                side_effect=sqlite_failure(sqlite3.SQLITE_IOERR_WRITE),
            ) as commit,
            mock.patch.object(store, "heal_hot_journal") as heal,
        ):
            self.assert_unavailable(*self.propose())
        self.assertEqual(commit.call_count, 1)
        heal.assert_not_called()
        self.assertEqual(len(connections), 2)
        self.assert_closed(connections)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 2)

    def test_unhealable_reader_refuses_after_one_connection(self) -> None:
        for module, target, argv in (
            (cli_know, "_views", ("know", "list")),
            (cli_learn, "_detail", ("learn", "show", self.context.request_id)),
        ):
            with (
                self.subTest(command=argv),
                mock.patch.object(
                    store, "heal_hot_journal", return_value=False
                ) as heal,
                mock.patch.object(
                    module,
                    target,
                    side_effect=sqlite_failure(
                        sqlite3.SQLITE_READONLY_ROLLBACK
                    ),
                ) as work,
            ):
                code, out = self.call(*argv)
                self.assertEqual((code, out["error"]), (4, "hot_journal"))
                self.assertEqual(work.call_count, 1)
                self.assertEqual(heal.call_count, 1)
