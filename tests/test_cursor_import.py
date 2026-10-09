"""Cursor history is imported as part of a full ingest pass."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

from muninn import cursor_import_read
from muninn.cursor_import import default_database
from tests.cli_support import CliCase


class CursorImportTests(CliCase):
    """Exercise Cursor discovery and import without installing Cursor."""

    def database_path(self) -> Path:
        """Return Cursor's standard database path under the test HOME."""
        path = Path(self.env["HOME"]) / "synthetic Cursor/state.vscdb"
        self.env["MUNINN_CURSOR_DB"] = str(path)
        selected = default_database(Path(self.env["HOME"]), env=self.env)
        assert selected is not None
        return selected

    def make_database(self) -> tuple[Path, bytes]:
        """Create a supported synthetic Cursor database."""
        path = self.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        composer = {
            "_v": 17,
            "fullConversationHeadersOnly": [
                {"bubbleId": "user-1"},
                {"bubbleId": "assistant-1"},
            ],
        }
        bubbles = [
            (
                "bubbleId:composer-1:user-1",
                {"type": 1, "text": "cursor zebra prompt"},
            ),
            (
                "bubbleId:composer-1:assistant-1",
                {"type": 2, "text": "cursor zebra answer"},
            ),
        ]
        conn.execute(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            ("composerData:composer-1", json.dumps(composer)),
        )
        conn.executemany(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in bubbles],
        )
        conn.commit()
        conn.close()
        return path, path.read_bytes()

    def test_full_ingest_imports_cursor_and_is_idempotent(self) -> None:
        path, original = self.make_database()

        self.assertEqual(self.muninn("ingest", "--full")[0], 0)
        self.assertEqual(path.read_bytes(), original)
        code, searched, _ = self.muninn(
            "search", "zebra", "--provider", "cursor", "--all-projects"
        )
        self.assertEqual(code, 0)
        texts = {
            self.muninn("open", hit["ref"])[1]["text"]
            for hit in searched["hits"]
        }
        self.assertEqual(texts, {"cursor zebra prompt", "cursor zebra answer"})

        self.assertEqual(self.muninn("ingest", "--full")[0], 0)
        code, stats, _ = self.muninn("stats")
        self.assertEqual(code, 0)
        self.assertEqual(stats["events_by_provider"]["cursor"], 2)
        self.conn.close()
        code, rebuilt, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, rebuilt)

    def test_invalid_cursor_input_refuses_before_ingest(self) -> None:
        with mock.patch("muninn.cli_maint.ingest.run_pass") as run:
            code, output, _ = self.muninn(
                "ingest", "--full", env={"MUNINN_CURSOR_DB": "relative"}
            )
        self.assertEqual(code, 2, output)
        run.assert_not_called()

    def test_regular_ingest_does_not_import_cursor(self) -> None:
        self.make_database()
        self.assertEqual(self.muninn("ingest")[0], 0)
        code, stats, _ = self.muninn("stats")
        self.assertEqual(code, 0)
        self.assertEqual(stats["events_by_provider"].get("cursor", 0), 0)

    def test_precompact_read_skips_oversized_conversations_whole(self) -> None:
        path, _ = self.make_database()
        conversations, skipped, _ = cursor_import_read.read(
            path, max_bubbles=1, conversation_id="composer-1"
        )
        self.assertEqual(conversations, [])
        self.assertEqual(skipped, 1)

    def test_full_ingest_skips_a_missing_cursor_database(self) -> None:
        self.assertEqual(self.muninn("ingest", "--full")[0], 0)
        code, stats, _ = self.muninn("stats")
        self.assertEqual(code, 0)
        self.assertEqual(stats["events_by_provider"].get("cursor", 0), 0)

    def test_empty_home_uses_path_home_fallback(self) -> None:
        home = Path(self.env["HOME"])
        self.make_database()
        with mock.patch("muninn.cli_maint.Path.home", return_value=home):
            code, _, _ = self.muninn("ingest", "--full", env={"HOME": ""})
        self.assertEqual(code, 0)
        code, stats, _ = self.muninn("stats")
        self.assertEqual(code, 0)
        self.assertEqual(stats["events_by_provider"]["cursor"], 2)

    def test_erased_cursor_content_stays_erased_on_full_ingest(self) -> None:
        path, _ = self.make_database()
        self.assertEqual(self.muninn("ingest", "--full")[0], 0)
        code, erased, _ = self.muninn(
            "erase", "--session", "composer-1", "--yes"
        )
        self.assertEqual(code, 0)
        self.assertIn(str(path), erased["provider_files"])
        self.assertEqual(self.muninn("ingest", "--full")[0], 0)
        code, stats, _ = self.muninn("stats")
        self.assertEqual(code, 0)
        self.assertEqual(stats["events_by_provider"].get("cursor", 0), 0)

    def test_unsupported_database_error_does_not_expose_transcript_text(
        self,
    ) -> None:
        path = self.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE other (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO other VALUES ('secret', 'private transcript')"
        )
        conn.commit()
        conn.close()

        code, answer, _ = self.muninn("ingest", "--full")
        self.assertEqual(code, 2)
        self.assertEqual(answer["error"], "refused")
        self.assertNotIn("private transcript", json.dumps(answer))
        calls = (self.home / "calls.jsonl").read_text()
        self.assertNotIn("private transcript", calls)
