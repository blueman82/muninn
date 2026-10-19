"""Disposable fixtures for interrupted erasure and process reopening."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

from muninn import erase, query, store, tombstones
from muninn.erase_collect import Target
from muninn.review_quiescence import EraseStatus
from tests.cli_support import CliCase
from tests.review_quiescence_support import initialize

TARGET = "replay-target"
CONTROL = "replay-control"
CANARY = "replayerasurecanary"


def interrupted(home: Path, selector: str, value: str) -> None:
    """Exit after the public erase has durably appended its actual intent."""
    status = EraseStatus()

    def cut(conn: sqlite3.Connection, target: Target) -> None:
        assert status.erasure_phase == "intent_durable"
        assert not conn.in_transaction
        assert target.events
        os._exit(71)

    if selector == "session":
        request = erase.EraseRequest(session=value)
    elif selector == "match":
        request = erase.EraseRequest(match=value)
    else:
        request = erase.EraseRequest(event_ref=value)
    with mock.patch.object(erase, "_commit", cut):
        erase.run_erase_request(home, request, wait_s=0, status=status)
    raise AssertionError("interruption_not_reached")


class ReplayCase(CliCase):
    """Keep all provider and database material inside the temporary fixture."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)

    def child(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the fixture module with disposable application roots."""
        return subprocess.run(
            [sys.executable, "-B", "-m", __name__, *args],
            env=self.env,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def interrupt(
        self, selector: str = "session", value: str = TARGET
    ) -> bytes:
        """Preserve genuine durable intent before any deletion transaction."""
        self.conn.close()
        result = self.child("interrupt", str(self.home), selector, value)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (71, "", "")
        )
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)
        return (self.home / "tombstones.jsonl").read_bytes()

    def count(self, table: str) -> int:
        """Count rows in a fixture-selected table."""
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def replay(self) -> int:
        """Replay under a real caller-owned writer and connection."""
        with store.writer_lock(self.home, wait_s=0):
            return tombstones.reapply_tombstones(self.conn, self.home)


if __name__ == "__main__":
    if sys.argv[1] == "interrupt":
        interrupted(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        data_home = Path(sys.argv[2])
        with closing(store.connect_ro(store.db_path(data_home))) as connection:
            tombstones.require_current_tombstones(connection, data_home)
            answer = query.search(
                connection,
                CANARY,
                cwd=os.environ["HOME"],
                env=os.environ,
                all_projects=True,
                include_current=True,
            )
            assert not answer["hits"]
