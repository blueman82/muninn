"""Exact Cursor identity and workspace binding against synthetic databases."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from typing import Any
from unittest import mock

from muninn import cursor_import, hook_cursor
from tests.cli_support import CliCase
from tests.hook_support import launcher_args


class CursorPrecompactTests(CliCase):
    """A hook must never import a different conversation or workspace."""

    def setUp(self) -> None:
        super().setUp()
        self.database = self.tmp / "cursor.vscdb"
        self.env["MUNINN_CURSOR_DB"] = str(self.database)
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
            conn.commit()
        self.add_conversation("active")

    def add_conversation(self, identity: str) -> None:
        """Append a synthetic composer and two uniquely identified bubbles."""
        composer = {
            "composerId": identity,
            "fullConversationHeadersOnly": [
                {"bubbleId": "one"},
                {"bubbleId": "two"},
            ],
        }
        rows = [(f"composerData:{identity}", json.dumps(composer))]
        rows += [
            (
                f"bubbleId:{identity}:{bubble}",
                json.dumps({"type": role, "text": f"{identity} {bubble}"}),
            )
            for bubble, role in (("one", 1), ("two", 2))
        ]
        with contextlib.closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)

    def payload(self, **changes: Any) -> dict[str, Any]:
        """Return a documented hook payload bound to the isolated project."""
        return {
            "conversation_id": "active",
            "workspace_roots": [str(self.repo)],
            "cwd": str(self.repo),
            "message_count": 2,
        } | changes

    def rows(self) -> list[tuple[Any, ...]]:
        """Read event identity, scope and content from the isolated store."""
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT s.thread_id,e.cwd,e.scope_id,e.text FROM event e"
                " JOIN source s ON s.id=e.source_id ORDER BY s.thread_id,e.seq"
            )
        ]

    def test_command_imports_matching_older_conversation_only(self) -> None:
        self.add_conversation("foreign-latest")
        done = subprocess.run(
            launcher_args("hook", "pre-compact", "--provider", "cursor"),
            input=json.dumps(self.payload()).encode(),
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout.strip()), (0, b"{}"))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[0] for row in rows}, {"active"})
        self.assertEqual({row[1] for row in rows}, {str(self.repo.resolve())})

    def test_bad_or_missing_identity_never_opens_writer(self) -> None:
        for value in (None, False, 42, "", " ", "\0", "unknown"):
            with (
                self.subTest(value=value),
                mock.patch.object(
                    cursor_import.store, "writer_lock"
                ) as writer,
            ):
                out = hook_cursor.pre_compact(
                    self.payload(conversation_id=value), "cursor", self.env
                )
                self.assertIn("user_message", out)
                writer.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_duplicate_and_mismatched_composer_identity_skip(self) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                with (
                    contextlib.closing(sqlite3.connect(self.database)) as conn,
                    conn,
                ):
                    conn.execute(
                        "DELETE FROM cursorDiskKV"
                        " WHERE key='composerData:active'"
                    )
                    raw = json.dumps({"composerId": "other"})
                    conn.execute(
                        "INSERT INTO cursorDiskKV VALUES (?, ?)",
                        ("composerData:active", raw),
                    )
                    if duplicate:
                        conn.execute(
                            "INSERT INTO cursorDiskKV VALUES (?, ?)",
                            ("composerData:active", raw),
                        )
                with mock.patch.object(
                    cursor_import.store, "writer_lock"
                ) as writer:
                    out = hook_cursor.pre_compact(
                        self.payload(), "cursor", self.env
                    )
                    self.assertIn("user_message", out)
                    writer.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_invalid_workspace_roots_skip_before_writes(self) -> None:
        for roots in (None, [], ["relative"], [1], ["\0"], "not-list"):
            with (
                self.subTest(roots=roots),
                mock.patch.object(
                    cursor_import.store, "writer_lock"
                ) as writer,
            ):
                out = hook_cursor.pre_compact(
                    self.payload(workspace_roots=roots), "cursor", self.env
                )
                self.assertIn("user_message", out)
                writer.assert_not_called()

    def test_single_workspace_overrides_stale_cursor_environment(self) -> None:
        out = hook_cursor.pre_compact(
            self.payload(cwd=str(self.tmp / "other")),
            "cursor",
            self.env | {"CURSOR_PROJECT_DIR": str(self.tmp / "stale")},
        )
        self.assertEqual(out, {})
        self.assertEqual({row[1] for row in self.rows()}, {str(self.repo)})

    def test_multiple_workspaces_require_a_matching_candidate(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        roots = [str(self.repo), str(other)]
        with mock.patch.object(cursor_import.store, "writer_lock") as writer:
            out = hook_cursor.pre_compact(
                self.payload(workspace_roots=roots),
                "cursor",
                self.env | {"CURSOR_PROJECT_DIR": str(self.tmp / "foreign")},
            )
            self.assertIn("user_message", out)
            writer.assert_not_called()
        child = self.repo / "child"
        child.mkdir()
        out = hook_cursor.pre_compact(
            self.payload(workspace_roots=roots),
            "cursor",
            self.env | {"CURSOR_PROJECT_DIR": str(child)},
        )
        self.assertEqual(out, {})
        self.assertEqual({row[1] for row in self.rows()}, {str(child)})

    def test_oversized_refresh_preserves_complete_stored_conversation(
        self,
    ) -> None:
        cursor_import.run(self.home, self.database, str(self.repo), 1)
        before = self.rows()
        with mock.patch.object(cursor_import.store, "writer_lock") as writer:
            out = hook_cursor.pre_compact(
                self.payload(message_count=1), "cursor", self.env
            )
            self.assertEqual(out, {})
            writer.assert_not_called()
        self.assertEqual(self.rows(), before)

    def test_exact_refresh_corrects_unchanged_foreign_scope(self) -> None:
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        self.add_conversation("foreign-latest")
        cursor_import.run(self.home, self.database, str(foreign), 1)
        before = self.rows()
        self.assertEqual(
            hook_cursor.pre_compact(self.payload(), "cursor", self.env), {}
        )
        rows = self.rows()
        active = [row for row in rows if row[0] == "active"]
        self.assertEqual({row[1] for row in active}, {str(self.repo)})
        self.assertNotEqual(active[0][2], before[0][2])
        self.assertEqual(
            [row for row in rows if row[0] == "foreign-latest"],
            [row for row in before if row[0] == "foreign-latest"],
        )

    def test_scope_rewrite_preserves_erased_event(self) -> None:
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        cursor_import.run(self.home, self.database, str(foreign), 1)
        code, answer, _ = self.muninn(
            "erase", "--event", "cursor:active:1.1", "--yes"
        )
        self.assertEqual(code, 0, answer)
        self.assertEqual(
            hook_cursor.pre_compact(self.payload(), "cursor", self.env), {}
        )
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], str(self.repo))
        self.assertEqual(rows[0][3], "active two")
