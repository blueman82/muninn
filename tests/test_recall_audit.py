"""Recall checkpoints survive restarts and count only bounded deliveries."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import recall_audit, store
from tests.store_support import assert_private


class RecallAuditTests(unittest.TestCase):
    """Private transactions and sequential review acknowledgments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        store.ensure_private_dir(self.home)
        self.payload: dict[str, object] = {
            "prompt": "CANARY private request words",
            "session_id": "session-one",
            "muninn_provider": "claude",
        }

    def capture(self, text: str | None = None) -> tuple[str, dict[str, Any]]:
        """Record a synthetic delivery with assessment instructions."""
        trace: dict[str, Any] = {
            "delivered_sources": [{"ref": "codex:t:1.1", "cap": 120}]
        }
        out = recall_audit.capture(
            self.home,
            self.payload,
            [1],
            lambda hint: hint if text is None else text,
            trace,
        )
        return out, trace

    def test_counts_only_nonempty_deliveries_without_text(self) -> None:
        self.capture("")
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["total"], 0
        )
        self.capture()
        report = recall_audit.review(self.home, None, ack=False)
        self.assertEqual(report["total"], 1)
        self.assertFalse(report["complete"])
        delivery = report["deliveries"][0]
        self.assertEqual(delivery["sources"][0]["ref"], "codex:t:1.1")
        self.assertEqual(delivery["session"], "session-one")
        self.assertNotIn("CANARY", json.dumps(report))
        self.assertNotIn(
            b"CANARY", (self.home / recall_audit.NAME).read_bytes()
        )
        assert_private(self, self.home / recall_audit.NAME)

    def test_fiftieth_delivery_reminds_until_acknowledged(self) -> None:
        for _ in range(49):
            text, _ = self.capture()
            self.assertNotIn("Review due", text)
        text, trace = self.capture()
        self.assertEqual(trace["delivery_id"], 50)
        self.assertIn("Review due", text)
        text, _ = self.capture()
        self.assertIn("Review due", text)
        report = recall_audit.review(self.home, None, ack=False)
        self.assertEqual(len(report["deliveries"]), 50)
        self.assertTrue(report["complete"])
        recall_audit.review(self.home, 1, ack=True)
        text, _ = self.capture()
        self.assertNotIn("Review due", text)
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["batch"], 2
        )

    def test_ack_requires_next_complete_batch(self) -> None:
        self.capture()
        with self.assertRaises(ValueError):
            recall_audit.review(self.home, 1, ack=True)
        for _ in range(99):
            self.capture()
        with self.assertRaises(ValueError):
            recall_audit.review(self.home, 2, ack=True)
        recall_audit.review(self.home, 1, ack=True)
        recall_audit.review(self.home, 2, ack=True)
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["batch"], 2
        )
        with self.assertRaises(ValueError):
            recall_audit.review(self.home, 2, ack=True)

    def test_absent_review_is_read_only_and_invalid_batches_fail(self) -> None:
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["total"], 0
        )
        self.assertFalse((self.home / recall_audit.NAME).exists())
        self.capture()
        for batch in (0, -1, 2):
            with self.assertRaises(ValueError):
                recall_audit.review(self.home, batch, ack=False)

    def test_concurrent_deliveries_get_unique_ids(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.capture(), range(20)))
        ids = [trace["delivery_id"] for _, trace in results]
        self.assertEqual(sorted(ids), list(range(1, 21)))
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["total"], 20
        )

    def test_locked_or_corrupt_audit_fails_open(self) -> None:
        with mock.patch.object(
            recall_audit,
            "connection",
            side_effect=sqlite3.OperationalError("busy"),
        ):
            text, trace = self.capture()
        self.assertIn("review unavailable", text)
        self.assertEqual(trace["review_error"], 1)
        self.assertNotIn("delivery_id", trace)
        (self.home / recall_audit.NAME).write_text("bad database")
        text, trace = self.capture()
        self.assertTrue(text)
        self.assertEqual(trace["review_error"], 1)

    def test_failed_commit_does_not_publish_a_delivery_id(self) -> None:
        with mock.patch.object(
            recall_audit.json, "dumps", side_effect=OSError("full")
        ):
            _, trace = self.capture()
        self.assertNotIn("delivery_id", trace)
        self.capture()
        self.assertEqual(
            recall_audit.review(self.home, None, ack=False)["total"], 1
        )

    def test_read_refuses_a_symlink(self) -> None:
        target = self.home / "other.sqlite"
        target.write_text("unchanged")
        link = self.home / recall_audit.NAME
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(OSError):
            recall_audit.review(self.home, None, ack=False)
        _, trace = self.capture()
        self.assertEqual(trace["review_error"], 1)
        self.assertEqual(target.read_text(), "unchanged")
