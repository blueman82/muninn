"""Shared fixtures for the ingest tests.

Synthetic provider trees in temp dirs only; the record builders mirror the
real key structure (see tests/test_classify.py).  Nothing touches a live
data dir or provider root.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pctx import ingest, store
from tests.test_classify import codex_meta, reply, user_msg

Record = dict[str, Any]

ROOT = Path(__file__).resolve().parent.parent
TID = "0199aaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BASE = "0199bbbb-0000-4000-8000-000000000001"
SEG = "0199bbbb-0000-4000-8000-000000000002"

# Run in a child process so a second process can contend for the writer lock.
HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pctx import store
with store.writer_lock(Path(sys.argv[2]), wait_s=0):
    print("ready", flush=True)
    time.sleep(30)
"""

FAILED = "Chunk ID: a\nWall time: 0.1 seconds\nProcess exited with code {}\n"


def rollout(tid: str = TID) -> str:
    """Return the relative path of a Codex rollout file for a thread.

    Args:
        tid: Thread id embedded in the file name.

    Returns:
        Path relative to the Codex sessions root.
    """
    return f"2026/01/02/rollout-2026-01-02T03-04-05-{tid}.jsonl"


def line(record: Record) -> bytes:
    """Serialise one record as a compact JSONL line.

    Args:
        record: JSON-serialisable record.

    Returns:
        The encoded line, newline included.
    """
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def primary(tid: str = TID) -> list[Record]:
    """Return a minimal primary thread: metadata, one prompt, one reply.

    Args:
        tid: Thread id of the session metadata.

    Returns:
        The three records.
    """
    return [codex_meta("user", tid), user_msg(1, "q one"), reply(2, "a one")]


def fc_output(ordinal: int, call_id: str, text: str) -> Record:
    """Return a function_call_output record.

    Args:
        ordinal: Provider ordinal of the record.
        call_id: Id of the call this output answers.
        text: Output text.

    Returns:
        The record.
    """
    payload = {
        "type": "function_call_output",
        "call_id": call_id,
        "id": "o",
        "output": text,
        "internal_chat_message_metadata_passthrough": {},
    }
    return {
        "timestamp": "t",
        "type": "response_item",
        "ordinal": ordinal,
        "payload": payload,
    }


class IngestCase(unittest.TestCase):
    """Base case with a temp store and synthetic provider roots."""

    def setUp(self) -> None:
        """Create the temp home, provider roots and an open store."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.roots = {
            "codex-sessions": self.tmp / "codex" / "sessions",
            "codex-archived": self.tmp / "codex" / "archived_sessions",
            "claude-projects": self.tmp / "claude" / "projects",
        }
        for path in self.roots.values():
            path.mkdir(parents=True)
        self.conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        self.addCleanup(self.conn.close)
        self.tick = 1_700_000_000_000_000_000

    def run_ingest(
        self, *, only_threads: set[str] | None = None, full: bool = False
    ) -> ingest.PassStats:
        """Run one ingest pass over the synthetic roots.

        Args:
            only_threads: Restrict the pass to these thread ids.
            full: Replace instead of append for every source.

        Returns:
            The pass statistics.
        """
        return ingest.ingest(
            self.conn, self.roots, only_threads=only_threads, full=full
        )

    def write(
        self,
        rel: str,
        records: list[Record],
        root: str = "codex-sessions",
        tail: bytes = b"",
    ) -> Path:
        """Write a JSONL file under a provider root.

        Args:
            rel: Path relative to the root.
            records: Records, one per line.
            root: Name of the provider root.
            tail: Raw bytes appended after the last line.

        Returns:
            The written path.
        """
        path = self.roots[root] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(map(line, records)) + tail)
        self.bump(path)
        return path

    def append(
        self, path: Path, records: list[Record], tail: bytes = b""
    ) -> None:
        """Append records to an existing JSONL file.

        Args:
            path: File to extend.
            records: Records, one per line.
            tail: Raw bytes appended after the last line.
        """
        with path.open("ab") as handle:
            handle.write(b"".join(map(line, records)) + tail)
        self.bump(path)

    def bump(self, path: Path) -> None:
        """Give a file a strictly newer mtime.

        A rewrite must never look unchanged to the change detector.

        Args:
            path: File whose mtime is advanced.
        """
        self.tick += 1_000_000_000
        os.utime(path, ns=(self.tick, self.tick))

    def events(self, thread_id: str = TID) -> list[tuple[int, int, str, str]]:
        """Return the indexed events of a thread.

        Args:
            thread_id: Thread to read.

        Returns:
            Tuples of (line, part, kind, text) in file order.
        """
        return [
            (r["line"], r["part"], r["kind"], r["text"])
            for r in self.conn.execute(
                "SELECT e.* FROM event e JOIN source s ON s.id = e.source_id"
                " WHERE s.thread_id = ? ORDER BY e.line, e.part",
                (thread_id,),
            )
        ]

    def source(self, thread_id: str = TID) -> sqlite3.Row | None:
        """Return the source row of a thread, or None when absent.

        Args:
            thread_id: Thread to look up.

        Returns:
            The row, or None.
        """
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM source WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row

    def need_source(self, thread_id: str = TID) -> sqlite3.Row:
        """Return the source row of a thread that must exist.

        Args:
            thread_id: Thread to look up.

        Returns:
            The row.
        """
        row = self.source(thread_id)
        assert row is not None
        return row
