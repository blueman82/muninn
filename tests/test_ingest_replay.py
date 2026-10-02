"""Ingest of erased, replayed and continued threads.

Tombstones, fork and subagent replay skipping, symlink handling, locking and
crash rollback, plus rollout files that continue one long thread.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import classify, ingest, store
from tests.ingest_support import (
    BASE,
    HOLD_LOCK,
    ROOT,
    SEG,
    TID,
    IngestCase,
    line,
    primary,
    rollout,
)
from tests.test_classify import (
    PARENT,
    claude_rec,
    codex_meta,
    reply,
    subagent_meta,
    user_msg,
)


class TombstoneAndReplayTests(IngestCase):
    """Erased data stays erased; replayed history is not indexed twice."""

    def tombstone(self, level: str, **ids: str | int) -> None:
        """Insert a tombstone row.

        Args:
            level: Tombstone level: thread, session or line.
            **ids: Scope columns of the tombstone, such as ``thread_id``.
        """
        cols = ", ".join(["created_at", "provider", "level", *ids])
        marks = ", ".join("?" * (3 + len(ids)))
        self.conn.execute(
            f"INSERT INTO tombstone({cols}) VALUES ({marks})",
            (0, "codex", level, *ids.values()),
        )

    def test_tombstoned_thread_never_reingested(self) -> None:
        path = self.write(rollout(), primary())
        self.tombstone("thread", thread_id=TID)
        stats = self.run_ingest()
        self.assertEqual(stats.skipped_files, 1)
        self.assertIsNone(self.source())
        path.rename(self.roots["codex-archived"] / path.name)
        self.run_ingest(full=True)
        self.assertIsNone(self.source())  # also after an archive move
        # a session tombstone covers the session's threads and its forks
        self.write(rollout("thr-s"), primary("thr-s"))
        fork = codex_meta("user", "thr-f", forked_from_id="thr-s")
        self.write(rollout("thr-f"), [fork, user_msg(1, "own")])
        self.tombstone("session", session_root="thr-s")
        self.run_ingest()
        rows = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_tombstoned_line_skipped(self) -> None:
        records = primary()
        path = self.write(rollout(), records)
        erased = path.read_bytes().split(b"\n")[1]  # line 2, "q one"
        self.tombstone(
            "line",
            thread_id=TID,
            line=2,
            line_sha256=classify.record_hash(erased),
        )
        self.run_ingest()
        self.assertEqual(self.events(), [(3, 1, "reply", "a one")])
        self.run_ingest(full=True)  # a replace still skips it
        self.assertEqual(self.events(), [(3, 1, "reply", "a one")])

    def test_content_prefix_replay_skipped_for_old_user_fork(self) -> None:
        parent = [
            codex_meta("user", PARENT),
            user_msg(1, "parent q"),
            reply(2, "parent a"),
        ]
        fork_meta = codex_meta("user", "thr-old", forked_from_id=PARENT)
        fork = [
            fork_meta,
            user_msg(1, "parent q"),
            reply(2, "parent a"),
            user_msg(3, "fork q"),
            reply(4, "parent a"),
        ]
        self.write(rollout("thr-old"), fork)  # discovered before its parent
        self.write(rollout(PARENT), parent)
        stats = self.run_ingest()
        # The parent is parsed first, so the fork needs only one parse.
        self.assertEqual(stats.events_removed, 0)
        self.assertEqual(
            self.need_source("thr-old")["replay_mode"], "content_prefix"
        )
        self.assertEqual(
            self.events("thr-old"),
            [(4, 1, "prompt", "fork q"), (5, 1, "reply", "parent a")],
        )

    def test_content_prefix_unverified_until_parent_appears(self) -> None:
        fork_meta = codex_meta("user", "thr-old", forked_from_id=PARENT)
        fork = [fork_meta, user_msg(1, "parent q"), user_msg(2, "fork q")]
        self.write(rollout("thr-old"), fork)
        self.run_ingest()
        self.assertEqual(
            self.need_source("thr-old")["replay_mode"], "unverified"
        )
        self.assertEqual(len(self.events("thr-old")), 2)  # indexed normally
        self.write(
            rollout(PARENT),
            [codex_meta("user", PARENT), user_msg(1, "parent q")],
        )
        self.run_ingest()  # the fork file itself is unchanged
        self.assertEqual(
            self.need_source("thr-old")["replay_mode"], "content_prefix"
        )
        self.assertEqual(self.events("thr-old"), [(3, 1, "prompt", "fork q")])

    def test_ordinal_replay_skipped(self) -> None:
        records = [
            subagent_meta("thr-sub", k=3),
            codex_meta("user", PARENT, ordinal=1),
            user_msg(2, "replayed parent prompt"),
            user_msg(3, "the task"),
            reply(4, "done"),
        ]
        self.write(rollout("thr-sub"), records)
        self.run_ingest()
        self.assertEqual(
            self.events("thr-sub"),
            [(4, 1, "delegation", "the task"), (5, 1, "reply", "done")],
        )
        src = self.need_source("thr-sub")
        self.assertEqual(
            (src["replay_mode"], src["replay_before"]), ("ordinal", 3)
        )

    def test_symlinks_and_nonregular_skipped(self) -> None:
        real = self.write(rollout(), primary())
        other = self.tmp / "elsewhere.jsonl"
        other.write_bytes(b"".join(map(line, primary("thr-link"))))
        (real.parent / "rollout-x-thr-link.jsonl").symlink_to(other)
        os.mkfifo(real.parent / "rollout-fifo.jsonl")
        (real.parent / "rollout-dir.jsonl").mkdir()
        linked = self.roots["codex-sessions"] / "2027"
        linked.symlink_to(self.tmp / "codex")  # a symlinked directory
        stats = self.run_ingest()
        self.assertEqual(stats.files_seen, 1)
        threads = [
            r[0] for r in self.conn.execute("SELECT thread_id FROM source")
        ]
        self.assertEqual(threads, [TID])

    def test_no_change_pass_reads_nothing(self) -> None:
        self.write(rollout(), primary())
        self.write(
            "-work-repo/sess-1.jsonl",
            [claude_rec("user", "hi")],
            root="claude-projects",
        )
        self.run_ingest()
        # The pass opens files with Path.open, which bypasses builtins.open.
        with mock.patch.object(
            Path, "open", autospec=True, side_effect=Path.open
        ) as opened:
            stats = self.run_ingest()
        self.assertEqual(opened.call_count, 0)
        self.assertEqual((stats.files_seen, stats.files_changed), (2, 0))

    def test_concurrent_ingest_one_skips(self) -> None:
        self.write(rollout(), primary())
        holder = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                HOLD_LOCK,
                str(ROOT),
                str(self.home),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        out = holder.stdout
        assert out is not None
        self.assertEqual(out.readline().strip(), "ready")
        out.close()
        with self.assertRaises(store.BusyError):
            ingest.run_pass(self.home, self.roots, fullfsync=False)
        holder.kill()
        holder.wait()
        stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(stats.events_added, 2)

    def test_crash_mid_source_rolls_back(self) -> None:
        self.write(rollout("0000-bad"), primary("0000-bad"))
        self.write(rollout(), primary())
        real = ingest._commit_source

        def boom(
            conn: sqlite3.Connection, src_id: int, *args: Any, **kw: Any
        ) -> Any:
            """Fail the commit of one source after its events are inserted."""
            thread = conn.execute(
                "SELECT thread_id FROM source WHERE id = ?", (src_id,)
            ).fetchone()[0]
            if thread == "0000-bad":  # events are already inserted
                raise sqlite3.OperationalError("simulated crash")
            return real(conn, src_id, *args, **kw)

        with mock.patch.object(ingest, "_commit_source", boom):
            stats = self.run_ingest()
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.errors, {"OperationalError": 1})
        self.assertIsNone(self.source("0000-bad"))  # nothing committed
        self.assertEqual(len(self.events()), 2)  # the other source landed
        again = self.run_ingest()  # the next pass redoes the source
        self.assertEqual(len(self.events("0000-bad")), 2)
        self.assertEqual(again.failed, 0)


class SegmentTests(IngestCase):
    """A long Codex thread continues in new rollout files.

    The continuation keeps the same payload id, takes a new rollout uuid in
    the file name, carries a history_base and continues the ordinals.
    """

    def files(self) -> None:
        """Write a base rollout and its continuation segment."""
        self.write(
            rollout(BASE),
            [codex_meta("user", BASE), user_msg(1, "early"), reply(2, "e")],
        )
        hb = {
            "end_byte_offset": 10,
            "end_ordinal_exclusive": 3,
            "thread_id": BASE,
        }
        seg = codex_meta("user", BASE, ordinal=3, history_base=hb)
        self.write(rollout(SEG), [seg, user_msg(4, "later"), reply(5, "l")])

    def test_continuation_segments_are_separate_sources(self) -> None:
        self.files()
        stats = self.run_ingest()
        self.assertEqual((stats.failed, stats.skipped_files), (0, 0))
        rows = self.conn.execute(
            "SELECT thread_id, session_root, parent_thread_id, replay_mode"
            " FROM source ORDER BY thread_id"
        )
        self.assertEqual(
            [tuple(r) for r in rows],
            [(BASE, BASE, None, "none"), (SEG, BASE, BASE, "history_base")],
        )
        self.assertEqual([e[3] for e in self.events(SEG)], ["later", "l"])
        again = self.run_ingest()
        self.assertEqual((again.files_changed, again.skipped_files), (0, 0))

    def test_segments_follow_their_thread_for_tombstones_and_targets(
        self,
    ) -> None:
        self.files()
        targeted = self.run_ingest(only_threads={BASE})
        self.assertEqual(targeted.files_changed, 2)  # all of the thread
        self.conn.execute("DELETE FROM event")  # what an erase leaves behind
        self.conn.execute("DELETE FROM source")
        self.conn.execute(
            "INSERT INTO tombstone(created_at, provider, level, thread_id)"
            " VALUES (0, 'codex', 'thread', ?)",
            (BASE,),
        )
        self.run_ingest()
        count = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(count, 0)
