"""Verify automatic recall excludes raw user-message echoes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
context = import_module("context")


class HookRoleTest(unittest.TestCase):
    """Keep automatic context to historic assistant evidence."""

    def test_assistant_filter_excludes_user_echoes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            database = Path(temporary) / "context.sqlite"
            records = [
                {
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": "coderails provenance user-echo",
                        "cwd": "/repo",
                    }
                },
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "coderails provenance assistant answer",
                        "cwd": "/repo",
                    }
                },
            ]
            (root / "session.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n"
            )
            context.build_index(root, database)
            with sqlite3.connect(database) as connection:
                automatic = context.evidence_packet_from_connection(
                    connection,
                    "coderails provenance",
                    "/repo",
                    1_800,
                    role="assistant",
                )
                manual = context.evidence_packet_from_connection(
                    connection,
                    "coderails provenance",
                    "/repo",
                    1_800,
                )
            self.assertIn("assistant answer", json.dumps(automatic))
            self.assertNotIn("user-echo", json.dumps(automatic))
            self.assertTrue(manual["evidence"])


if __name__ == "__main__":
    unittest.main()
