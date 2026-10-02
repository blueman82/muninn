"""Ingest contract: source modes, provider roots, races and copies.

Synthetic provider trees in temp dirs only; the record builders mirror the
real key structure (see tests/test_classify.py).  Nothing touches a live
data dir or provider root.  The shared fixtures live in
tests/ingest_support.py and are re-exported here for the other test modules.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from muninn import classify, ingest, ingest_model, ingest_plan
from tests.ingest_support import (
    BASE,
    HOLD_LOCK,
    ROOT,
    SEG,
    TID,
    IngestCase,
    fc_output,
    line,
    primary,
    rollout,
)
from tests.test_classify import CWD, codex_meta, reply, user_msg

__all__ = [
    "BASE",
    "HOLD_LOCK",
    "ROOT",
    "SEG",
    "TID",
    "IngestCase",
    "fc_output",
    "line",
    "primary",
    "rollout",
]


class SourceModeTests(IngestCase):
    """How a source is first indexed, appended to, replaced or retired."""

    def test_new_primary_source_ingested(self) -> None:
        path = self.write(rollout(), primary())
        stats = self.run_ingest()
        self.assertEqual((stats.files_seen, stats.files_changed), (1, 1))
        self.assertEqual(stats.events_added, 2)
        self.assertEqual(
            self.events(),
            [(2, 1, "prompt", "q one"), (3, 1, "reply", "a one")],
        )
        src = self.need_source()
        self.assertEqual(
            (src["provider"], src["thread_class"], src["root"], src["path"]),
            ("codex", "primary", "codex-sessions", rollout()),
        )
        raw = path.read_bytes().split(b"\n")
        rows = self.conn.execute(
            "SELECT line, byte_offset, line_sha256, seq, ts, cwd, scope_id"
            " FROM event ORDER BY line"
        ).fetchall()
        for row in rows:  # identity is the record hash of the line
            begin = sum(len(r) + 1 for r in raw[: row["line"] - 1])
            self.assertEqual(row["byte_offset"], begin)
            self.assertEqual(
                row["line_sha256"], classify.record_hash(raw[row["line"] - 1])
            )
            self.assertEqual((row["seq"], row["cwd"]), (row["line"] - 1, CWD))
        self.assertEqual(src["cursor_bytes"], path.stat().st_size)
        self.assertEqual(src["cursor_line"], 3)
        # Default search must never surface a non-primary thread.
        leaks = self.conn.execute(
            "SELECT count(*) FROM event e JOIN source s ON s.id = e.source_id"
            " WHERE e.kind IN ('prompt','reply','tool_call')"
            " AND s.thread_class != 'primary'"
        ).fetchone()[0]
        self.assertEqual(leaks, 0)

    def test_non_primary_source_row_only_no_events(self) -> None:
        handoff = [codex_meta("chatgpt_handoff", "thr-h"), user_msg(1, "x")]
        unknown = codex_meta("user", "thr-u")
        del unknown["payload"]["thread_source"]
        self.write(rollout("thr-h"), handoff)
        self.write(rollout("thr-u"), [unknown, user_msg(1, "y")])
        self.run_ingest()
        rows = self.conn.execute(
            "SELECT thread_id, thread_class, class_reason, cursor_bytes"
            " FROM source ORDER BY thread_id"
        )
        self.assertEqual(
            [tuple(r) for r in rows],
            [
                ("thr-h", "other", "thread_source=chatgpt_handoff", 0),
                ("thr-u", "other", "thread_source=missing", 0),
            ],
        )
        count = self.conn.execute("SELECT count(*) FROM event").fetchone()[0]
        self.assertEqual(count, 0)

    def test_append_mode_adds_only_new_lines(self) -> None:
        path = self.write(rollout(), primary())
        self.run_ingest()
        first_ids = [r[0] for r in self.conn.execute("SELECT id FROM event")]
        self.append(path, [user_msg(3, "q two"), reply(4, "a two")])
        stats = self.run_ingest()
        self.assertEqual((stats.events_added, stats.events_removed), (2, 0))
        self.assertEqual(
            [e[3] for e in self.events()], ["q one", "a one", "q two", "a two"]
        )
        ids = [r[0] for r in self.conn.execute("SELECT id FROM event")]
        self.assertEqual(ids[:2], first_ids)  # rows already indexed are kept
        again = self.run_ingest()
        self.assertEqual((again.files_changed, again.events_added), (0, 0))
        self.assertEqual(len(self.events()), 4)

    def test_same_size_inplace_rewrite_triggers_replace(self) -> None:
        for what in ("first line", "anchor line"):
            with self.subTest(what=what):
                tid = f"thr-rw-{what[0]}"
                records = primary(tid)
                path = self.write(rollout(tid), records)
                self.run_ingest()
                if what == "first line":  # same id, same length, new bytes
                    records[0]["payload"]["cwd"] = CWD[:-1] + "X"
                else:
                    records[2] = reply(2, "a onX")
                size = path.stat().st_size
                self.write(rollout(tid), records)
                self.assertEqual(path.stat().st_size, size)
                stats = self.run_ingest()
                self.assertEqual(
                    (stats.events_removed, stats.events_added), (2, 2)
                )
                want = "a onX" if what == "anchor line" else "a one"
                self.assertEqual(self.events(tid)[-1][3], want)

    def test_truncation_replaces(self) -> None:
        records = [*primary(), user_msg(3, "q two")]
        path = self.write(rollout(), records)
        self.run_ingest()
        self.write(rollout(), records[:2])
        stats = self.run_ingest()
        self.assertEqual(self.events(), [(2, 1, "prompt", "q one")])
        self.assertEqual(stats.events_removed, 3)
        self.assertEqual(
            self.need_source()["cursor_bytes"], path.stat().st_size
        )

    def test_archive_move_same_thread_id_updates_path_no_duplicate(
        self,
    ) -> None:
        path = self.write(rollout(), primary())
        self.run_ingest()
        ids = [r[0] for r in self.conn.execute("SELECT id FROM event")]
        target = self.roots["codex-archived"] / path.name
        path.rename(target)
        stats = self.run_ingest()
        rows = self.conn.execute("SELECT root, path, status FROM source")
        self.assertEqual(
            [tuple(r) for r in rows], [("codex-archived", path.name, "active")]
        )
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT id FROM event")], ids
        )
        self.assertEqual((stats.events_added, stats.missing), (0, 0))
        # a copy (both paths present) is not indexed twice
        os.link(target, path)
        dup = self.run_ingest()
        self.assertEqual(dup.skipped_files, 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM source").fetchone()[0], 1
        )

    def test_deleted_source_marked_missing_events_kept(self) -> None:
        path = self.write(rollout(), primary())
        self.run_ingest()
        path.unlink()
        stats = self.run_ingest()
        self.assertEqual(stats.missing, 1)
        self.assertEqual(self.need_source()["status"], "missing")
        self.assertEqual(len(self.events()), 2)
        self.write(rollout(), primary())
        self.run_ingest()
        self.assertEqual(self.need_source()["status"], "active")
        targeted = self.run_ingest(only_threads={"someone-else"})
        self.assertEqual(targeted.missing, 0)

    def test_oversize_line_skipped_source_continues(self) -> None:
        path = self.write(rollout(), primary())
        with path.open("ab") as handle:
            handle.write(b'{"pad":"' + b"x" * 5000 + b'"}\n')
        self.append(path, [user_msg(4, "after the big one")])
        with mock.patch.object(ingest_model, "MAX_LINE_BYTES", 4096):
            stats = self.run_ingest()
        self.assertEqual(stats.skipped_lines, 1)
        self.assertEqual(
            self.events()[-1], (5, 1, "prompt", "after the big one")
        )
        issue = self.conn.execute("SELECT line, code FROM source_issue")
        self.assertEqual([tuple(r) for r in issue], [(4, "line_too_large")])
        self.assertEqual(self.need_source()["skipped_lines"], 1)

    def test_invalid_json_line_skipped(self) -> None:
        path = self.write(rollout(), primary())
        deep = "[" * 1100 + "]" * 1100
        with path.open("ab") as handle:
            handle.write(b"{not json\n[1, 2]\n" + deep.encode() + b"\n")
        self.append(path, [user_msg(7, "still here")])
        stats = self.run_ingest()
        codes = self.conn.execute("SELECT line, code FROM source_issue")
        self.assertEqual(
            [tuple(r) for r in codes],
            [(4, "invalid_json"), (5, "not_object"), (6, "too_deep")],
        )
        self.assertEqual(stats.skipped_lines, 3)
        self.assertEqual(self.events()[-1][3], "still here")

    def test_partial_tail_deferred_then_committed_once(self) -> None:
        partial = line(user_msg(3, "typed so far"))[:-1]  # no newline yet
        path = self.write(rollout(), primary(), tail=partial)
        self.run_ingest()
        self.assertEqual(len(self.events()), 2)
        committed = self.need_source()["cursor_bytes"]
        # The cursor stops before the unterminated line, so it is re-read
        # whole once the writer finishes it.
        self.assertEqual(committed, path.stat().st_size - len(partial))
        with path.open("ab") as handle:
            handle.write(b"\n")
        self.bump(path)
        self.run_ingest()
        self.run_ingest()
        self.assertEqual(
            [e[3] for e in self.events()], ["q one", "a one", "typed so far"]
        )


class RootsAndPassTests(unittest.TestCase):
    """Provider root discovery from the environment."""

    def test_default_roots(self) -> None:
        roots = ingest.default_roots({"HOME": "/h"})
        self.assertEqual(
            roots,
            {
                "codex-sessions": Path("/h/.codex/sessions"),
                "codex-archived": Path("/h/.codex/archived_sessions"),
                "claude-projects": Path("/h/.claude/projects"),
            },
        )
        override = json.dumps({"claude-projects": "~/x"})
        got = ingest.default_roots({"HOME": "/h", "MUNINN_ROOTS": override})
        self.assertEqual(got, {"claude-projects": Path("/h/x")})
        for bad in ('{"nope": "/x"}', "not json", '["codex-sessions"]'):
            env = {"HOME": "/h", "MUNINN_ROOTS": bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ingest.default_roots(env)


class RaceTests(IngestCase):
    """Files changing underneath a running pass."""

    def test_file_vanishing_mid_pass_is_skipped(self) -> None:
        gone = self.write(rollout("thr-gone"), primary("thr-gone"))
        self.write(rollout(), primary())
        real = ingest_plan._first_line

        def racing(path: Path) -> bytes | None:
            """Delete the doomed file just before it is first read."""
            if path == gone:
                gone.unlink()  # archived or deleted after discovery
            return real(path)

        with mock.patch.object(ingest_plan, "_first_line", racing):
            stats = self.run_ingest()
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(len(self.events()), 2)


class CopyTests(IngestCase):
    """The same thread present under two roots."""

    def test_same_pass_copies_indexed_once(self) -> None:
        self.write(rollout(), primary())
        self.write(Path(rollout()).name, primary(), root="codex-archived")
        stats = self.run_ingest()
        self.assertEqual((stats.failed, stats.skipped_files), (0, 1))
        count = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(len(self.events()), 2)
