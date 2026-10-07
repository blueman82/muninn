"""Generation-bound private stop requests and transaction-safe polling."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from muninn import cli_serve, platform_io, poller_stop, store

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
