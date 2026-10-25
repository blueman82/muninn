"""Managed-writer proof helpers require transaction and native evidence."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from install.context import Ctx, run_real
from muninn import review_pause_state as state
from muninn import store
from tests import lifecycle_native_writer
from tests.review_pause_support import initialize


class NativeWriterProofTests(unittest.TestCase):
    """Keep journal and committed-row evidence separate from process state."""

    def test_crash_proof_waits_for_replacement_exit(self) -> None:
        def service(**overrides: str) -> bytes:
            fields = {
                "NRestarts": "1",
                "ExecMainPID": "101",
                "ExecMainCode": "1",
                "ExecMainStatus": "1",
                "ExecMainExitTimestampMonotonic": "12345",
                "MainPID": "0",
                "Result": "exit-code",
            }
            fields.update(overrides)
            return "".join(
                f"{key}={value}\n" for key, value in fields.items()
            ).encode()

        refused = lifecycle_native_writer.replacement_refused
        self.assertFalse(refused(service(ExecMainPID="100"), 100))
        self.assertFalse(
            refused(
                service(
                    ExecMainPID="100",
                    ExecMainCode="2",
                    ExecMainStatus="9",
                    Result="signal",
                ),
                100,
            )
        )
        self.assertFalse(
            refused(
                service(
                    ExecMainCode="0",
                    ExecMainStatus="0",
                    ExecMainExitTimestampMonotonic="0",
                    MainPID="101",
                    Result="success",
                ),
                100,
            )
        )
        self.assertTrue(refused(service(), 100))

    def test_writer_verification_reports_service_failure_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "busy", platform="linux")
            with (
                patch.object(
                    lifecycle_native_writer,
                    "service_status",
                    return_value=(False, "writer_command_mismatch"),
                ),
                self.assertRaisesRegex(
                    AssertionError, "writer_command_mismatch"
                ),
            ):
                lifecycle_native_writer._verified(ctx)

    @unittest.skipIf(sys.platform == "win32", "POSIX interpreter symlinks")
    def test_busy_restart_preserves_recorded_virtualenv_interpreter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(
                Path(temporary).resolve(), run_real, "busy", platform="linux"
            )
            executable = ctx.home / "base-python"
            executable.write_bytes(b"synthetic executable")
            recorded = ctx.lib / "release/.venv/bin/python3"
            recorded.parent.mkdir(parents=True)
            recorded.symlink_to(executable)
            (ctx.lib / "python").symlink_to(recorded)

            def start(ctx: Ctx, python: Path, release: Path) -> None:
                rendered = lifecycle_native_writer.lifecycle.linux_unit(
                    ctx, python, release
                )
                self.assertTrue(
                    lifecycle_native_writer.obs_linux_service.unit_matches(
                        rendered,
                        recorded,
                        release,
                        ctx.data,
                        {
                            "HOME": str(ctx.home),
                            "MUNINN_HOME": str(ctx.data),
                            "CODEX_HOME": str(ctx.codex_home),
                            "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
                        },
                    )
                )
                raise LookupError("restart verified")

            with (
                patch.object(lifecycle_native_writer.sys, "platform", "linux"),
                patch.object(
                    lifecycle_native_writer.lifecycle, "start", start
                ),
                self.assertRaisesRegex(LookupError, "restart verified"),
            ):
                lifecycle_native_writer.busy_stop(ctx)

    @unittest.skipIf(sys.platform == "win32", "POSIX native writer fixture")
    def test_busy_reader_starts_after_actual_owner_startup_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(
                Path(temporary).resolve(), run_real, "busy", platform="linux"
            )
            store.connect_rw(store.db_path(ctx.data)).close()
            initialize(ctx.data)

            def start(ctx: Ctx, python: Path, release: Path) -> None:
                descriptor, token = state.begin_owner(ctx.data)
                state.complete_owner(ctx.data, descriptor, token)
                os.close(descriptor)

            with (
                patch.object(lifecycle_native_writer.sys, "platform", "linux"),
                patch.object(
                    Path, "readlink", return_value=Path(sys.executable)
                ),
                patch.object(
                    lifecycle_native_writer.lifecycle, "start", start
                ),
                patch.object(
                    lifecycle_native_writer,
                    "wait",
                    side_effect=LookupError("startup reached"),
                ),
                self.assertRaisesRegex(LookupError, "startup reached"),
            ):
                lifecycle_native_writer.busy_stop(ctx)

    def test_busy_proof_refuses_outside_native_linux_before_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "busy", platform="linux")
            with (
                patch.object(
                    lifecycle_native_writer.sys, "platform", "darwin"
                ),
                patch.object(
                    lifecycle_native_writer.lifecycle, "start"
                ) as start,
                self.assertRaisesRegex(OSError, "native_linux_required"),
            ):
                lifecycle_native_writer.busy_stop(ctx)
            start.assert_not_called()
            self.assertFalse(ctx.data.exists())

    def test_actual_journal_requires_nonempty_transaction_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            self.assertFalse(lifecycle_native_writer.journal_ready(data))
            journal = data / "muninn.sqlite-journal"
            journal.write_bytes(b"")
            self.assertFalse(lifecycle_native_writer.journal_ready(data))
            journal.write_bytes(b"\x00" * 512)
            self.assertTrue(lifecycle_native_writer.journal_ready(data))

    def test_row_proof_counts_only_the_selected_synthetic_session(
        self,
    ) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE source(id INTEGER, session_root TEXT)")
            conn.execute("CREATE TABLE event(source_id INTEGER)")
            conn.executemany(
                "INSERT INTO source VALUES(?,?)", ((1, "wanted"), (2, "other"))
            )
            conn.executemany("INSERT INTO event VALUES(?)", ((1,), (1,), (2,)))
            self.assertEqual(
                lifecycle_native_writer.committed_rows(conn, "wanted"), 2
            )
            self.assertEqual(
                lifecycle_native_writer.committed_rows(conn, "missing"), 0
            )
