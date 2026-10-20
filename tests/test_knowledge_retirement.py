"""Retain CLI durability, refusal and privacy after ledger removal."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from muninn import cli_core, store
from muninn.obs_log import clean_record
from muninn.store_copy import copy_governed_state
from tests.test_cli_learning import CliLearningCase


class WriterBoundaryTests(CliLearningCase):
    """Exercise normalized commands and caller-owned connection lifetime."""

    def retract(self, ident: str) -> tuple[int, dict[str, object]]:
        """Run the explicit normalized withdrawal command."""
        return self.call(
            "know",
            "retract",
            ident,
            "--expected-revision",
            "1",
            "--reason",
            "Synthetic withdrawal",
        )

    def test_writers_lock_fullsync_close_and_release(self) -> None:
        connect = store.connect_rw
        opened: list[sqlite3.Connection] = []

        def checked(path: Path, fullfsync: bool = True) -> sqlite3.Connection:
            self.assertTrue(fullfsync)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.home, wait_s=0),
            ):
                self.fail("writer not owned")
            conn = connect(path, fullfsync=fullfsync)
            self.assertEqual(conn.execute("PRAGMA fullfsync").fetchone()[0], 1)
            opened.append(conn)
            return conn

        with mock.patch.object(store, "connect_rw", side_effect=checked):
            code, result = self.propose()
            self.assertEqual(code, 0)
            ident = result["applied"][0]["knowledge_id"]
            self.assertEqual(self.retract(ident)[0], 0)
        self.assertEqual(len(opened), 3)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        with store.writer_lock(self.home, wait_s=0):
            pass
        self.assertEqual(cli_core.WRITER_WAIT_S, 15.0)
        self.assertEqual(
            self.row("SELECT state FROM knowledge")[0], "retracted"
        )

    def test_refusal_releases_lock_and_writes_no_knowledge(self) -> None:
        code, result = self.propose(evidence_refs=["E_" + "0" * 32])
        self.assertEqual((code, result["error"]), (2, "rejected_evidence"))
        code, result = self.retract("K_" + "0" * 32)
        self.assertEqual(code, 2)
        self.assertIn("error", result)
        with store.writer_lock(self.home, wait_s=0):
            pass
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_both_writers_report_busy_without_mutation(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli_core, "WRITER_WAIT_S", 0),
        ):
            for action in (
                self.propose,
                lambda: self.retract("K_" + "0" * 32),
            ):
                code, result = action()
                self.assertEqual((code, result["error"]), (3, "busy"))
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_actual_call_logs_hold_ids_and_codes_without_content(self) -> None:
        code, receipt = self.propose(content="Synthetic zebra decision.")
        self.assertEqual(code, 0)
        ident = receipt["applied"][0]["knowledge_id"]
        self.call("know", "list")
        self.retract("K_" + "0" * 32)
        text = (self.home / "calls.jsonl").read_text()
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [row["cmd"] for row in rows],
            ["know propose", "know list", "know retract"],
        )
        self.assertEqual(rows[0]["knowledge_ids"], [ident])
        self.assertIn("error", rows[2])
        for secret in ("zebra", "Synthetic withdrawal", "Use Python"):
            self.assertNotIn(secret, text)

    def test_revision_history_survives_fresh_process_after_retraction(
        self,
    ) -> None:
        code, receipt = self.propose()
        self.assertEqual(code, 0)
        ident = receipt["applied"][0]["knowledge_id"]
        self.context = self.start(2, 2)
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=ident,
                        expected_revision=1,
                        content="Use Python after review.",
                    )
                ]
            )
        )
        code, _ = self.call(
            "know",
            "retract",
            ident,
            "--expected-revision",
            "2",
            "--reason",
            "Withdraw after correction.",
        )
        self.assertEqual(code, 0)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from muninn.cli import main; raise SystemExit(main(sys.argv[2:]))"
        )
        for revision, text in (
            (1, "Use Python."),
            (2, "Use Python after review."),
        ):
            with self.subTest(revision=revision):
                done = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        script,
                        str(Path(__file__).resolve().parents[1]),
                        "know",
                        "show",
                        ident,
                        "--revision",
                        str(revision),
                    ],
                    cwd=self.cwd,
                    env=dict(
                        os.environ,
                        HOME=str(self.tmp / "userhome"),
                        MUNINN_HOME=str(self.home),
                    ),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                shown = json.loads(done.stdout)
                self.assertEqual(done.stderr, "")
                self.assertEqual(
                    (shown["id"], shown["revision"], shown["state"]),
                    (ident, revision, "retracted"),
                )
                self.assertEqual(shown["body"]["content"], text)
                self.assertEqual(
                    shown["body"]["evidence_refs"], [self.evidence[0].id]
                )
                self.assertEqual(
                    shown["retracted_reason"], "Withdraw after correction."
                )

    def test_silent_row_loss_refuses_and_rolls_back_current_copy(self) -> None:
        self.save(self.result([self.note()]))
        before = self.counts()
        with closing(
            store.connect_rw(self.tmp / "candidate.sqlite")
        ) as target:
            original = list(target.iterdump())
            target.execute(
                "CREATE TEMP TRIGGER lose BEFORE INSERT ON knowledge_version "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
            with self.assertRaisesRegex(
                ValueError, "cutover_checksum_mismatch"
            ):
                copy_governed_state(self.conn, target)
            self.assertEqual(list(target.iterdump()), original)
            self.assertEqual(
                target.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
            self.assertFalse(target.in_transaction)
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_corrupt_store_maps_read_and_write_commands_to_exit_four(
        self,
    ) -> None:
        self.conn.close()
        store.db_path(self.home).write_bytes(b"not a database" * 1000)
        for action in (
            lambda: self.call("know", "list"),
            self.propose,
            lambda: self.retract("K_" + "0" * 32),
        ):
            with self.subTest(action=action):
                code, result = action()
                self.assertEqual(
                    (code, result["error"]), (4, "store_unavailable")
                )

    def test_store_lost_after_connection_open_maps_to_exit_four(self) -> None:
        for command in ("list", "propose", "retract"):
            with self.subTest(command=command):
                self.check_midcall_loss(command)

    def check_midcall_loss(self, command: str) -> None:
        """Corrupt one real opened store and retain cleanup assertions."""
        case = CliLearningCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        name = "connect_ro" if command == "list" else "connect_rw"
        connect = getattr(store, name)
        opened: list[sqlite3.Connection] = []

        def corrupt(path: Path, **kwargs: object) -> sqlite3.Connection:
            conn = connect(path, **kwargs)
            opened.append(conn)
            path.write_bytes(b"not a database" * 1000)
            return conn

        try:
            with mock.patch.object(store, name, side_effect=corrupt):
                if command == "propose":
                    code, result = case.propose()
                elif command == "retract":
                    code, result = case.call(
                        "know",
                        "retract",
                        "K_" + "0" * 32,
                        "--expected-revision",
                        "1",
                        "--reason",
                        "x",
                    )
                else:
                    code, result = case.call("know", "list")
            self.assertEqual((code, result["error"]), (4, "store_unavailable"))
        finally:
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            with store.writer_lock(case.home, wait_s=0):
                pass


class DetailAllowlistTests(unittest.TestCase):
    """Real current-format refusal details survive ordinary log cleaning."""

    def test_known_store_messages_are_logged_whole(self) -> None:
        for message in (
            "cutover_required",
            "schema v7, need v2",
            "journal_mode is 'wal', need 'delete'",
            "cannot read the store (SQLITE_BUSY)",
            "cannot read the store (OperationalError)",
            "no store",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    clean_record({"detail": message}), {"detail": message}
                )
        self.assertEqual(clean_record({"detail": "x" * 81}), {})

    def test_real_unsupported_and_garbage_refusals_are_logged_whole(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "unsupported.sqlite"
            with closing(sqlite3.connect(old)) as conn:
                conn.execute("CREATE TABLE old(x)")
                conn.execute("PRAGMA user_version=1")
            old.chmod(0o600)
            garbage = Path(tmp) / "garbage.sqlite"
            garbage.write_bytes(b"not a database" * 100)
            garbage.chmod(0o600)
            for path in (old, garbage):
                with self.assertRaises(store.StoreUnavailableError) as caught:
                    store.connect_ro(path)
                message = str(caught.exception)[:80]
                self.assertEqual(
                    clean_record({"detail": message}), {"detail": message}
                )
