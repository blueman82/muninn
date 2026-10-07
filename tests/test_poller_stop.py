"""Generation-bound private stop requests and transaction-safe polling."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from muninn import cli_serve, platform_io, poller_stop, store
from tests.store_support import Child

GENERATION = "a" * 32


class PollerStopTests(unittest.TestCase):
    """Stale requests cannot stop a replacement process."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "private"
        platform_io.ensure_private_dir(self.home)

    def test_only_matching_pid_and_generation_stop(self) -> None:
        poller_stop.request(self.home, 123, GENERATION)
        self.assertTrue(poller_stop.requested(self.home, 123, GENERATION))
        self.assertFalse(poller_stop.requested(self.home, 124, GENERATION))
        self.assertFalse(poller_stop.requested(self.home, 123, "b" * 32))

    def test_malformed_and_extra_fields_do_not_stop(self) -> None:
        for value in (
            None,
            {"pid": True, "generation": GENERATION},
            {"pid": 123, "generation": GENERATION, "extra": 1},
            {"pid": 123, "generation": "bad"},
        ):
            with self.subTest(value=value):
                store.write_json_atomic(self.home / poller_stop.FILE, value)
                self.assertFalse(
                    poller_stop.requested(self.home, 123, GENERATION)
                )

    def test_invalid_request_arguments_create_nothing(self) -> None:
        with self.assertRaises(ValueError):
            poller_stop.request(self.home, 123, "bad")
        self.assertFalse((self.home / poller_stop.FILE).exists())

    def test_idle_wait_responds_before_long_interval(self) -> None:
        def write_request() -> None:
            time.sleep(0.1)
            poller_stop.request(self.home, os.getpid(), GENERATION)

        writer = threading.Thread(target=write_request)
        writer.start()
        try:
            began = time.monotonic()
            self.assertTrue(
                poller_stop.wait(self.home, os.getpid(), GENERATION, 5)
            )
            self.assertLess(time.monotonic() - began, 3)
        finally:
            writer.join(timeout=5)
        self.assertFalse(writer.is_alive())

    def test_stop_request_is_observed_after_commit(
        self,
    ) -> None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        proxy = cli_serve._StopAfterCommit(conn, requested=lambda: True)
        proxy.execute("CREATE TABLE test(value INTEGER)")
        proxy.execute("BEGIN IMMEDIATE")
        proxy.execute("INSERT INTO test VALUES(1)")
        with self.assertRaises(cli_serve._ServeStopError):
            proxy.execute("COMMIT")
        self.assertFalse(conn.in_transaction)
        self.assertEqual(
            conn.execute("SELECT value FROM test").fetchone(), (1,)
        )

    def test_busy_writer_commits_before_actual_process_exit(self) -> None:
        child = Child(
            self,
            """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from muninn import cli_serve, poller_stop, store
home, generation = Path(sys.argv[2]), sys.argv[3]
with store.writer_lock(home):
    conn = store.connect_rw(store.db_path(home), fullfsync=False)
    proxy = cli_serve._StopAfterCommit(
        conn, requested=lambda: poller_stop.requested(
            home, os.getpid(), generation
        )
    )
    proxy.execute("CREATE TABLE stopped_tx(value INTEGER)")
    proxy.execute("BEGIN IMMEDIATE")
    proxy.execute("INSERT INTO stopped_tx VALUES(1)")
    print("ready", flush=True)
    deadline = time.monotonic() + 20
    while not poller_stop.requested(home, os.getpid(), generation):
        if time.monotonic() >= deadline:
            raise RuntimeError("stop request did not arrive")
        time.sleep(0.05)
    try:
        proxy.execute("COMMIT")
    except cli_serve._ServeStopError:
        pass
    else:
        raise RuntimeError("writer did not stop after commit")
    conn.close()
""",
            self.home,
            GENERATION,
        )
        child.wait_ready(timeout=20)
        poller_stop.request(self.home, child.proc.pid, GENERATION)
        out, err = child.proc.communicate(timeout=5)
        self.assertEqual((child.proc.returncode, out, err), (0, "", ""))
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM stopped_tx").fetchone()[0], 1
            )
