"""Managed-writer proof helpers require transaction and native evidence."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from install.context import Ctx, run_real
from muninn import review_quiescence_state as state
from muninn import store
from tests import lifecycle_native_writer
from tests.review_quiescence_support import initialize


class NativeWriterProofTests(unittest.TestCase):
    """Keep journal and committed-row evidence separate from process state."""

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
