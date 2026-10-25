"""Keep erasure milestones alongside existing CLI success and errors."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, cast
from unittest import mock

from muninn import cli, erase, poller_stop, store, tombstone_key, tombstones
from muninn import review_pause_state as state
from muninn.review_pause import EraseStatus
from tests.cli_support import CliCase
from tests.review_pause_support import initialize


class CliErasePauseTests(CliCase):
    """Expose truthful status without changing other commands."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)

    def request(self, status: EraseStatus) -> dict[str, object]:
        """Run the explicit status-aware entry against this disposable home."""
        return erase.run_erase_request(
            self.home,
            erase.EraseRequest(session="missing"),
            env=self.env,
            status=status,
            wait_s=0,
        )

    def test_dry_run_reports_not_started_and_keeps_status_out_of_log(
        self,
    ) -> None:
        code, out, error = self.muninn("erase", "--session", "missing")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(
            out.get("erasure_status"),
            {
                "stop_may_have_been_published": False,
                "repair_phase": "none",
                "erasure_phase": "not_started",
            },
        )
        self.assertNotIn(
            "erasure_status", (self.home / "calls.jsonl").read_text()
        )

    def test_real_erase_reports_verified(self) -> None:
        code, out, error = self.muninn(
            "erase", "--session", "missing", "--yes"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(
            out.get("erasure_status"),
            {
                "stop_may_have_been_published": False,
                "repair_phase": "none",
                "erasure_phase": "verified",
            },
        )

    def test_existing_error_mapper_preserves_exact_status_identity(
        self,
    ) -> None:
        errors = (
            (store.BusyError("busy"), 3, {"error": "busy"}),
            (store.HotJournalError("hot"), 4, {"error": "hot_journal"}),
            (
                tombstone_key.TombstoneKeyError("key"),
                4,
                {"error": "tombstone_key"},
            ),
            (
                store.StoreUnavailableError("absent"),
                4,
                {"error": "store_unavailable", "detail": "absent"},
            ),
            (
                OSError("PRIVATEPATH"),
                4,
                {
                    "error": "store_unavailable",
                    "detail": "filesystem access is unsafe or unavailable",
                },
            ),
            (
                ValueError("refused"),
                2,
                {"error": "refused", "reason": "refused"},
            ),
            (
                LookupError("unknown"),
                2,
                {"error": "refused", "reason": "unknown"},
            ),
        )
        for error, expected_code, fields in errors:
            status = EraseStatus()

            def fail(
                home: Path,
                request: erase.EraseRequest,
                *,
                original_status: EraseStatus = status,
                original_error: Exception = error,
                **kwargs: Any,
            ) -> dict[str, object]:
                self.assertIs(kwargs["status"], original_status)
                original_status.stop_may_have_been_published = True
                original_status.repair_phase = "complete"
                original_status.erasure_phase = "intent_durable"
                raise original_error

            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    erase, "run_erase_request", side_effect=fail
                ),
                mock.patch.object(cli, "EraseStatus", return_value=status),
            ):
                code, out, stderr = self.muninn(
                    "erase", "--session", "missing", "--yes"
                )
            self.assertEqual((code, stderr), (expected_code, ""))
            self.assertEqual({key: out[key] for key in fields}, fields)
            self.assertEqual(
                out["erasure_status"],
                {
                    "stop_may_have_been_published": True,
                    "repair_phase": "complete",
                    "erasure_phase": "intent_durable",
                    "restart": "not_attempted",
                    "next_action": "check_and_restart_if_needed",
                },
            )
        self.assertNotIn(
            "PRIVATEPATH", (self.home / "calls.jsonl").read_text()
        )

    def test_arbitrary_sqlite_error_stays_uncaught(self) -> None:
        original = sqlite3.OperationalError("SQL failure")
        with (
            mock.patch.object(erase, "_commit", side_effect=original),
            self.assertRaises(sqlite3.Error) as caught,
        ):
            self.muninn("erase", "--session", "missing", "--yes")
        self.assertIs(caught.exception, original)

    def test_both_runners_preserve_body_error_when_connection_close_fails(
        self,
    ) -> None:
        connect = store.connect_rw
        original = LookupError("target not found")

        def failing(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def close() -> None:
                conn.close()
                raise OSError("close failed")

            proxy.close.side_effect = close
            return cast(sqlite3.Connection, proxy)

        for explicit, dry in (
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ):
            status = EraseStatus()
            with (
                self.subTest(explicit=explicit, dry=dry),
                mock.patch.object(store, "connect_rw", side_effect=failing),
                mock.patch.object(
                    erase, "collect_session", side_effect=original
                ),
                self.assertRaises(Exception) as caught,
            ):
                if explicit:
                    erase.run_erase_request(
                        self.home,
                        erase.EraseRequest(session="missing", dry_run=dry),
                        env=self.env,
                        status=status,
                        wait_s=0,
                    )
                else:
                    erase.run_erase(
                        self.home,
                        session="missing",
                        dry_run=dry,
                        env=self.env,
                        wait_s=0,
                    )
            self.assertIs(caught.exception, original)
            self.assertEqual(status.erasure_phase, "not_started")

    def test_append_commit_and_scan_failures_keep_latest_phase(self) -> None:
        for function, phase in (
            ("append_tombstones", "intent_may_be_durable"),
            ("_commit", "intent_durable"),
            ("residue_scan", "committed"),
        ):
            status = EraseStatus()
            original = OSError(function)
            with (
                self.subTest(function=function),
                mock.patch.object(erase, function, side_effect=original),
                self.assertRaises(OSError) as caught,
            ):
                self.request(status)
            self.assertIs(caught.exception, original)
            self.assertEqual(status.erasure_phase, phase)

    def test_rollback_failure_does_not_replace_commit_error(self) -> None:
        connect = store.connect_rw
        original = OSError("commit failed")

        def failing(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def execute(sql: str, *args: Any) -> sqlite3.Cursor:
                if (
                    sql == "COMMIT"
                    and (self.home / tombstones.TOMBSTONE_FILE).exists()
                ):
                    raise original
                if sql == "ROLLBACK":
                    conn.rollback()
                    raise OSError("rollback failed")
                return conn.execute(sql, *args)

            proxy.execute.side_effect = execute
            return cast(sqlite3.Connection, proxy)

        status = EraseStatus()
        with (
            mock.patch.object(store, "connect_rw", side_effect=failing),
            self.assertRaises(OSError) as caught,
        ):
            self.request(status)
        self.assertIs(caught.exception, original)
        self.assertEqual(status.erasure_phase, "intent_durable")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM tombstone").fetchone()[0],
            0,
        )

    def test_replay_rollback_keeps_original_error_before_requested_erase(
        self,
    ) -> None:
        tombstones.append_tombstones(
            self.home,
            [tombstones.tombstone_row("codex", "thread", thread_id="prior")],
        )
        journal = self.home / tombstones.TOMBSTONE_FILE
        before = journal.read_bytes()
        connect = store.connect_rw
        original = sqlite3.OperationalError("replay commit failed")

        def failing(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def execute(sql: str, *args: Any) -> sqlite3.Cursor:
                if sql == "COMMIT":
                    raise original
                if sql == "ROLLBACK":
                    conn.rollback()
                    raise OSError("replay rollback failed")
                return conn.execute(sql, *args)

            proxy.execute.side_effect = execute
            return cast(sqlite3.Connection, proxy)

        for use_cli in (False, True):
            status = EraseStatus()
            with self.subTest(use_cli=use_cli):
                with (
                    mock.patch.object(
                        store, "connect_rw", side_effect=failing
                    ),
                    mock.patch.object(cli, "EraseStatus", return_value=status),
                    mock.patch.object(
                        cli.cli_maint.cli_core, "WRITER_WAIT_S", 0
                    ),
                    self.assertRaises(Exception) as caught,
                ):
                    if use_cli:
                        self.muninn("erase", "--session", "missing", "--yes")
                    else:
                        self.request(status)
                self.assertIs(caught.exception, original)
                self.assertEqual(status, EraseStatus())
                self.assertEqual(journal.read_bytes(), before)
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM tombstone"
                    ).fetchone()[0],
                    0,
                )
                fd = state.acquire(self.home)
                try:
                    with store.writer_lock(self.home, wait_s=0):
                        pass
                finally:
                    os.close(fd)

    def test_residue_vocab_or_archive_never_reports_verified(self) -> None:
        archive = self.home / f"{store.PRE_UPGRADE_PREFIX}retained"
        archive.write_text("unrelated archive")
        status = EraseStatus()
        out = self.request(status)
        self.assertEqual(
            (out["residue"], status.erasure_phase), (0, "committed")
        )
        self.assertTrue(archive.exists())
        archive.unlink()
        for function, value in (
            ("vocab_left", 1),
            ("residue_scan", ["leftover"]),
        ):
            status = EraseStatus()
            with mock.patch.object(erase, function, return_value=value):
                self.request(status)
            self.assertEqual(status.erasure_phase, "committed")

    def test_zero_wait_keeps_busy_code_and_no_restart_advice(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli.cli_maint.cli_core, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.muninn(
                "erase", "--session", "missing", "--yes"
            )
        self.assertEqual((code, out["error"]), (3, "busy"))
        self.assertEqual(out["erasure_status"]["erasure_phase"], "not_started")
        self.assertNotIn("restart", out["erasure_status"])

    def test_invalid_selectors_never_stop_active_owner(self) -> None:
        fd, token = state.begin_owner(self.home)
        try:
            for args in (("--match", "a"), ("--event", "bad")):
                code, out, _ = self.muninn("erase", *args, "--yes")
                self.assertEqual(code, 2)
                self.assertFalse(
                    out["erasure_status"]["stop_may_have_been_published"]
                )
                self.assertFalse((self.home / "stop.json").exists())
        finally:
            state.complete_owner(self.home, fd, token)
            os.close(fd)

    def test_other_command_keeps_its_output_shape(self) -> None:
        code, out, _ = self.muninn("sessions")
        self.assertEqual(code, 0)
        self.assertNotIn("erasure_status", out)

    def test_absent_store_reports_refusal_and_default_status(self) -> None:
        absent = self.tmp / "absent"
        code, out, _ = self.muninn(
            "erase",
            "--session",
            "missing",
            "--yes",
            env={"MUNINN_HOME": str(absent)},
        )
        self.assertEqual(
            (code, out["error"], out["detail"]),
            (4, "store_unavailable", "store is absent"),
        )
        self.assertFalse(store.db_path(absent).exists())
        self.assertEqual(out["erasure_status"]["erasure_phase"], "not_started")
        self.assertNotIn("restart", out["erasure_status"])

    def test_visible_stop_failure_keeps_manual_recovery_and_no_intent(
        self,
    ) -> None:
        fd, token = state.begin_owner(self.home)
        publish = poller_stop.request

        def fail(*args: Any, **kwargs: Any) -> None:
            publish(*args, **kwargs)
            raise OSError("PRIVATEPATH")

        try:
            with mock.patch.object(poller_stop, "request", side_effect=fail):
                code, out, _ = self.muninn(
                    "erase", "--session", "missing", "--yes"
                )
            self.assertEqual(code, 4)
            self.assertTrue(
                poller_stop.requested(self.home, os.getpid(), token)
            )
            self.assertFalse((self.home / erase.TOMBSTONE_FILE).exists())
            self.assertEqual(
                out["erasure_status"],
                {
                    "stop_may_have_been_published": True,
                    "repair_phase": "none",
                    "erasure_phase": "not_started",
                    "restart": "not_attempted",
                    "next_action": "check_and_restart_if_needed",
                },
            )
        finally:
            state.complete_owner(self.home, fd, token)
            os.close(fd)
