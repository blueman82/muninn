"""Synthetic database and summary checks for the Cursor hook benchmark."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import benchmark_cursor_precompact as benchmark


class CursorPrecompactBenchmarkTests(unittest.TestCase):
    """The benchmark fixture contains only generated synthetic content."""

    def test_database_uses_requested_synthetic_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.vscdb"
            benchmark._create_database(path, 2, 3, 11)
            with sqlite3.connect(path) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM cursorDiskKV ORDER BY rowid"
                ).fetchall()
        composers = [key for key, _ in rows if key.startswith("composerData:")]
        bubbles = [key for key, _ in rows if key.startswith("bubbleId:")]
        self.assertEqual(len(composers), 3)
        self.assertEqual(len(bubbles), 7)
        active = json.loads(
            next(value for key, value in rows if key == "composerData:active")
        )
        self.assertEqual(len(active["fullConversationHeadersOnly"]), 3)
        active_bubbles = [
            json.loads(value)["text"]
            for key, value in rows
            if key.startswith("bubbleId:active:")
        ]
        self.assertTrue(all(len(text) == 11 for text in active_bubbles))

    def test_lock_holder_uses_the_store_native_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store" / "writer.lock"
            with patch.object(benchmark.store, "writer_lock") as writer_lock:
                self.assertEqual(benchmark._lock_holder(path, 0, 0), 0)
            writer_lock.assert_called_once_with(path.parent)

    def test_p95_uses_nearest_rank(self) -> None:
        self.assertEqual(benchmark._percentile95(list(range(1, 21))), 19)

    def test_p95_rejects_empty_samples(self) -> None:
        with self.assertRaises(ValueError):
            benchmark._percentile95([])

    def test_timeout_override_and_configured_default(self) -> None:
        with patch.object(benchmark, "_hook_timeout", return_value=60):
            self.assertEqual(benchmark._timeouts(None), (60, 60))
            self.assertEqual(benchmark._timeouts(90), (60, 90))
