"""Strict persisted intent validation over generated journals and stores."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from unittest import mock

from install.cutover import copy_governed_state
from muninn import store, tombstone_key, tombstones
from muninn.tombstone_intent import read_intent
from muninn.tombstone_replay import plan_transcripts
from tests.knowledge_cutover_support import legacy_schema
from tests.tombstone_replay_support import TARGET, ReplayCase


class TombstoneIntentTests(ReplayCase):
    """Unknown intent is never silently skipped or acknowledged complete."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.home / "tombstones.jsonl"
        self.row = tombstones.tombstone_row(
            "codex", "session", session_root=TARGET
        )

    def write_intent(self, data: bytes) -> None:
        """Create only private synthetic intent bytes."""
        self.path.write_bytes(data)
        self.path.chmod(0o600)

    def test_invalid_journal_is_atomic_and_content_free(self) -> None:
        self.session(TARGET, "synthetic canary")
        self.run_ingest()
        valid = json.dumps(self.row).encode() + b"\n"
        bad = [
            b"\xff\n",
            b"{}\n",
            b"[]\n",
            b"null\n",
            b"\n",
            valid[:-1],
            valid + b'{"provider":',
            b'{"provider":"codex","provider":"claude"}\n',
        ]
        changes = (
            {"created_at": float("nan")},
            {"created_at": float("inf")},
            {"created_at": True},
            {"line": True},
            {"line": -1},
            {"line": 2**63},
            {"session_root": "\ud800"},
            {"line_sha256": "ev2:bad"},
            {"provider": "unknown"},
            {"session_root": []},
            {"thread_id": {}},
            {"level": "file"},
            {"unexpected": "SYNTHETIC-PRIVATE-CONTENT"},
            {
                "level": "line",
                "thread_id": TARGET,
                "line": 0,
                "line_sha256": "a" * 64,
            },
        )
        bad.extend(
            json.dumps(self.row | change).encode() + b"\n"
            for change in changes
        )
        before = list(self.conn.iterdump())
        for data in bad:
            with self.subTest(data=data):
                self.write_intent(data)
                for operation in (
                    self.replay,
                    lambda: tombstones.require_current_tombstones(
                        self.conn, self.home
                    ),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "^erasure_intent_invalid$"
                    ):
                        operation()
                    self.assertEqual(list(self.conn.iterdump()), before)
                    self.assertFalse(self.conn.in_transaction)
                    self.assertEqual(self.path.read_bytes(), data)
                    self.assertFalse((self.home / "tombstone.key").exists())

    def test_json_string_line_separator_is_not_a_record_boundary(self) -> None:
        row = self.row | {"session_root": "synthetic\u2028identifier"}
        self.write_intent(json.dumps(row, ensure_ascii=False).encode() + b"\n")
        self.assertEqual(self.replay(), 1)
        self.assertEqual(self.replay(), 0)

    def test_duplicates_legacy_optional_metadata_and_crlf_are_valid(
        self,
    ) -> None:
        row = {"provider": "codex", "level": "session", "session_root": TARGET}
        data = b"".join(
            json.dumps(row | {"created_at": stamp}).encode() + b"\r\n"
            for stamp in (None, 0, -1, 10)
        )
        self.write_intent(data)
        self.assertEqual(self.replay(), 1)
        self.assertEqual(self.replay(), 0)
        self.assertEqual(self.count("tombstone"), 1)
        self.assertEqual(self.path.read_bytes(), data)

    def test_unsafe_or_unreadable_present_journal_never_means_absent(
        self,
    ) -> None:
        outside = self.tmp / "outside"
        outside.write_text(json.dumps(self.row) + "\n")
        self.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.replay()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(IsADirectoryError):
            self.replay()
        self.path.rmdir()
        self.write_intent(json.dumps(self.row).encode() + b"\n")
        original = PermissionError("synthetic read denied")
        with (
            mock.patch(
                "muninn.tombstone_intent.platform_io.open_regular",
                side_effect=original,
            ),
            self.assertRaises(PermissionError) as caught,
        ):
            self.replay()
        self.assertIs(caught.exception, original)
        self.assertEqual(self.count("tombstone"), 0)

    def test_required_missing_key_is_never_recreated(self) -> None:
        row = tombstones.tombstone_row(
            "codex",
            "line",
            thread_id=TARGET,
            line=0,
            line_sha256="ev2:" + "a" * 64,
        )
        tombstones.append_tombstones(self.home, [row])
        for operation in (
            self.replay,
            lambda: tombstones.require_current_tombstones(
                self.conn, self.home
            ),
        ):
            with self.assertRaises(tombstone_key.TombstoneKeyError):
                operation()
            self.assertFalse((self.home / "tombstone.key").exists())
            self.assertEqual(self.count("tombstone"), 0)

    def test_invalid_table_intent_refuses_without_a_journal(self) -> None:
        self.conn.execute(
            "INSERT INTO tombstone(created_at,provider,level,thread_id,line,"
            "line_sha256) VALUES(1,'codex','line',?,2,'bad')",
            (TARGET,),
        )
        with self.assertRaisesRegex(ValueError, "^erasure_intent_invalid$"):
            self.replay()
        self.assertFalse(self.path.exists())

    def test_legacy_pending_plan_is_read_only_and_candidate_replays(
        self,
    ) -> None:
        self.session(TARGET, "legacy secret")
        self.run_ingest()
        tombstones.append_tombstones(self.home, [self.row])
        path = self.tmp / "legacy.sqlite"
        with closing(sqlite3.connect(path, isolation_level=None)) as legacy:
            legacy.executescript(legacy_schema())
            for table in ("scope", "scope_path", "source", "event"):
                rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    marks = ",".join("?" for _ in rows[0])
                    legacy.executemany(
                        f"INSERT INTO {table} VALUES({marks})",
                        rows,
                    )
        before = path.read_bytes()
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, isolation_level=None
            )
        ) as legacy:
            intent = read_intent(legacy, self.home)
            plan = plan_transcripts(legacy, intent)
            self.assertEqual(
                (len(intent.missing), len(plan.sources), len(plan.events)),
                (1, 1, 1),
            )
            with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
                tombstones.require_current_tombstones(legacy, self.home)
            with closing(
                store.connect_rw(self.tmp / "candidate.sqlite")
            ) as candidate:
                copy_governed_state(legacy, candidate)
                self.assertEqual(
                    tombstones.reapply_tombstones(candidate, self.home), 1
                )
                self.assertEqual(
                    candidate.execute("SELECT count(*) FROM event").fetchone()[
                        0
                    ],
                    0,
                )
                tombstones.require_current_tombstones(candidate, self.home)
            self.write_intent(b'{"provider":\n')
            with self.assertRaisesRegex(
                ValueError, "^erasure_intent_invalid$"
            ):
                read_intent(legacy, self.home)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_ambiguous_family_refuses_without_source_mutation(
        self,
    ) -> None:
        self.session(TARGET, "ancestor history")
        self.session("uncovered-fork", "fork history")
        self.run_ingest()
        self.conn.execute(
            "UPDATE source SET forked_from_id=? WHERE thread_id=?",
            (TARGET, "uncovered-fork"),
        )
        tombstones.append_tombstones(self.home, [self.row])
        path = self.tmp / "ambiguous-legacy.sqlite"
        with closing(sqlite3.connect(path, isolation_level=None)) as legacy:
            legacy.executescript(legacy_schema())
            for table in ("scope", "scope_path", "source", "event"):
                rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    marks = ",".join("?" for _ in rows[0])
                    legacy.executemany(
                        f"INSERT INTO {table} VALUES({marks})", rows
                    )
        before = path.read_bytes()
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, isolation_level=None
            )
        ) as legacy:
            statements: list[str] = []
            legacy.set_trace_callback(statements.append)
            intent = read_intent(legacy, self.home)
            self.assertEqual(len(intent.missing), 1)
            with self.assertRaisesRegex(
                ValueError, "^erasure_history_unverified$"
            ):
                plan_transcripts(legacy, intent)
            self.assertTrue(all(s.startswith("SELECT ") for s in statements))
            self.assertFalse(legacy.in_transaction)
        self.assertEqual(path.read_bytes(), before)
