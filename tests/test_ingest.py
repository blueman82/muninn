"""Ingest contract (design 4.1, 3.5, 3.8; spec O1, O2, O7, O9, A3).

Synthetic provider trees in temp dirs only; the record builders mirror the
real key structure (see tests/test_classify.py).  Nothing touches a live
data dir or provider root.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pctx import classify, ingest, store
from tests.test_classify import (
    CWD,
    PARENT,
    agent_message,
    claude_rec,
    codex_meta,
    custom_call,
    function_call,
    reply,
    subagent_meta,
    text_block,
    tool_use,
    turn_context,
    user_msg,
)

ROOT = Path(__file__).resolve().parent.parent
TID = "0199aaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def rollout(tid=TID):
    return f"2026/01/02/rollout-2026-01-02T03-04-05-{tid}.jsonl"


def line(record):
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


class IngestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.roots = {
            "codex-sessions": self.tmp / "codex" / "sessions",
            "codex-archived": self.tmp / "codex" / "archived_sessions",
            "claude-projects": self.tmp / "claude" / "projects",
        }
        for path in self.roots.values():
            path.mkdir(parents=True)
        self.conn = store.connect_rw(store.db_path(self.home), fullfsync=False)
        self.addCleanup(self.conn.close)
        self.tick = 1_700_000_000_000_000_000

    def run_ingest(self, **kw):
        return ingest.ingest(self.conn, self.roots, **kw)

    def write(self, rel, records, root="codex-sessions", tail=b""):
        path = self.roots[root] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(map(line, records)) + tail)
        self.bump(path)
        return path

    def append(self, path, records, tail=b""):
        with open(path, "ab") as handle:
            handle.write(b"".join(map(line, records)) + tail)
        self.bump(path)

    def bump(self, path):
        """A strictly newer mtime, so a rewrite never looks unchanged."""
        self.tick += 1_000_000_000
        os.utime(path, ns=(self.tick, self.tick))

    def events(self, thread_id=TID):
        return [
            (r["line"], r["part"], r["kind"], r["text"])
            for r in self.conn.execute(
                "SELECT e.* FROM event e JOIN source s ON s.id = e.source_id"
                " WHERE s.thread_id = ? ORDER BY e.line, e.part",
                (thread_id,),
            )
        ]

    def source(self, thread_id=TID):
        return self.conn.execute(
            "SELECT * FROM source WHERE thread_id = ?", (thread_id,)
        ).fetchone()


def primary(tid=TID):
    return [codex_meta("user", tid), user_msg(1, "q one"), reply(2, "a one")]


class SourceModeTests(IngestCase):
    def test_new_primary_source_ingested(self):
        path = self.write(rollout(), primary())
        stats = self.run_ingest()
        self.assertEqual((stats.files_seen, stats.files_changed), (1, 1))
        self.assertEqual(stats.events_added, 2)
        self.assertEqual(
            self.events(),
            [(2, 1, "prompt", "q one"), (3, 1, "reply", "a one")],
        )
        src = self.source()
        self.assertEqual(
            (src["provider"], src["thread_class"], src["root"], src["path"]),
            ("codex", "primary", "codex-sessions", rollout()),
        )
        raw = path.read_bytes().split(b"\n")
        rows = self.conn.execute(
            "SELECT line, byte_offset, line_sha256, seq, ts, cwd, scope_id"
            " FROM event ORDER BY line"
        ).fetchall()
        for row in rows:  # I3: identity is the record hash of the line
            begin = sum(len(r) + 1 for r in raw[: row["line"] - 1])
            self.assertEqual(row["byte_offset"], begin)
            self.assertEqual(
                row["line_sha256"], classify.record_hash(raw[row["line"] - 1])
            )
            self.assertEqual((row["seq"], row["cwd"]), (row["line"] - 1, CWD))
        self.assertEqual(src["cursor_bytes"], path.stat().st_size)
        self.assertEqual(src["cursor_line"], 3)
        # I1 (O2 form): every default-eligible event is primary
        leaks = self.conn.execute(
            "SELECT count(*) FROM event e JOIN source s ON s.id = e.source_id"
            " WHERE e.kind IN ('prompt','reply','tool_call')"
            " AND s.thread_class != 'primary'"
        ).fetchone()[0]
        self.assertEqual(leaks, 0)

    def test_non_primary_source_row_only_no_events(self):
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

    def test_append_mode_adds_only_new_lines(self):
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
        self.assertEqual(ids[:2], first_ids)  # untouched rows (I6)
        again = self.run_ingest()
        self.assertEqual((again.files_changed, again.events_added), (0, 0))
        self.assertEqual(len(self.events()), 4)

    def test_same_size_inplace_rewrite_triggers_replace(self):
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

    def test_truncation_replaces(self):
        records = primary() + [user_msg(3, "q two")]
        path = self.write(rollout(), records)
        self.run_ingest()
        self.write(rollout(), records[:2])
        stats = self.run_ingest()
        self.assertEqual(self.events(), [(2, 1, "prompt", "q one")])
        self.assertEqual(stats.events_removed, 3)
        self.assertEqual(self.source()["cursor_bytes"], path.stat().st_size)

    def test_archive_move_same_thread_id_updates_path_no_duplicate(self):
        path = self.write(rollout(), primary())
        self.run_ingest()
        ids = [r[0] for r in self.conn.execute("SELECT id FROM event")]
        target = self.roots["codex-archived"] / path.name
        os.rename(path, target)
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

    def test_deleted_source_marked_missing_events_kept(self):
        path = self.write(rollout(), primary())
        self.run_ingest()
        path.unlink()
        stats = self.run_ingest()
        self.assertEqual(stats.missing, 1)
        self.assertEqual(self.source()["status"], "missing")
        self.assertEqual(len(self.events()), 2)
        self.write(rollout(), primary())
        self.run_ingest()
        self.assertEqual(self.source()["status"], "active")
        targeted = self.run_ingest(only_threads={"someone-else"})
        self.assertEqual(targeted.missing, 0)

    def test_oversize_line_skipped_source_continues(self):
        path = self.write(rollout(), primary())
        with open(path, "ab") as handle:
            handle.write(b'{"pad":"' + b"x" * 5000 + b'"}\n')
        self.append(path, [user_msg(4, "after the big one")])
        with mock.patch.object(ingest, "MAX_LINE_BYTES", 4096):
            stats = self.run_ingest()
        self.assertEqual(stats.skipped_lines, 1)
        self.assertEqual(
            self.events()[-1], (5, 1, "prompt", "after the big one")
        )
        issue = self.conn.execute("SELECT line, code FROM source_issue")
        self.assertEqual([tuple(r) for r in issue], [(4, "line_too_large")])
        self.assertEqual(self.source()["skipped_lines"], 1)

    def test_invalid_json_line_skipped(self):
        path = self.write(rollout(), primary())
        deep = "[" * 1100 + "]" * 1100
        with open(path, "ab") as handle:
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

    def test_partial_tail_deferred_then_committed_once(self):
        partial = line(user_msg(3, "typed so far"))[:-1]  # no newline yet
        path = self.write(rollout(), primary(), tail=partial)
        self.run_ingest()
        self.assertEqual(len(self.events()), 2)
        committed = self.source()["cursor_bytes"]
        self.assertEqual(committed, path.stat().st_size - len(partial))  # I4
        with open(path, "ab") as handle:
            handle.write(b"\n")
        self.bump(path)
        self.run_ingest()
        self.run_ingest()
        self.assertEqual(
            [e[3] for e in self.events()], ["q one", "a one", "typed so far"]
        )


HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pctx import store
with store.writer_lock(Path(sys.argv[2]), wait_s=0):
    print("ready", flush=True)
    time.sleep(30)
"""


class TombstoneAndReplayTests(IngestCase):
    def tombstone(self, level, **ids):
        cols = ", ".join(["created_at", "provider", "level", *ids])
        marks = ", ".join("?" * (3 + len(ids)))
        self.conn.execute(
            f"INSERT INTO tombstone({cols}) VALUES ({marks})",
            (0, "codex", level, *ids.values()),
        )

    def test_tombstoned_thread_never_reingested(self):
        path = self.write(rollout(), primary())
        self.tombstone("thread", thread_id=TID)
        stats = self.run_ingest()
        self.assertEqual(stats.skipped_files, 1)
        self.assertIsNone(self.source())
        os.rename(path, self.roots["codex-archived"] / path.name)
        self.run_ingest(full=True)
        self.assertIsNone(self.source())  # I5, also after an archive move
        # a session tombstone covers the session's threads and its forks
        self.write(rollout("thr-s"), primary("thr-s"))
        fork = codex_meta("user", "thr-f", forked_from_id="thr-s")
        self.write(rollout("thr-f"), [fork, user_msg(1, "own")])
        self.tombstone("session", session_root="thr-s")
        self.run_ingest()
        rows = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_tombstoned_line_skipped(self):
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

    def test_content_prefix_replay_skipped_for_old_user_fork(self):
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
        self.assertEqual(stats.events_removed, 0)  # parent first: one parse
        self.assertEqual(
            self.source("thr-old")["replay_mode"], "content_prefix"
        )
        self.assertEqual(
            self.events("thr-old"),
            [(4, 1, "prompt", "fork q"), (5, 1, "reply", "parent a")],
        )

    def test_content_prefix_unverified_until_parent_appears(self):
        fork_meta = codex_meta("user", "thr-old", forked_from_id=PARENT)
        fork = [fork_meta, user_msg(1, "parent q"), user_msg(2, "fork q")]
        self.write(rollout("thr-old"), fork)
        self.run_ingest()
        self.assertEqual(self.source("thr-old")["replay_mode"], "unverified")
        self.assertEqual(len(self.events("thr-old")), 2)  # indexed normally
        self.write(
            rollout(PARENT),
            [codex_meta("user", PARENT), user_msg(1, "parent q")],
        )
        self.run_ingest()  # the fork file itself is unchanged
        self.assertEqual(
            self.source("thr-old")["replay_mode"], "content_prefix"
        )
        self.assertEqual(self.events("thr-old"), [(3, 1, "prompt", "fork q")])

    def test_ordinal_replay_skipped(self):
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
        src = self.source("thr-sub")
        self.assertEqual(
            (src["replay_mode"], src["replay_before"]), ("ordinal", 3)
        )

    def test_symlinks_and_nonregular_skipped(self):
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

    def test_no_change_pass_reads_nothing(self):
        self.write(rollout(), primary())
        self.write(
            "-work-repo/sess-1.jsonl",
            [claude_rec("user", "hi")],
            root="claude-projects",
        )
        self.run_ingest()
        with mock.patch("builtins.open", wraps=open) as opened:
            stats = self.run_ingest()
        self.assertEqual(opened.call_count, 0)
        self.assertEqual((stats.files_seen, stats.files_changed), (2, 0))

    def test_concurrent_ingest_one_skips(self):
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
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        holder.stdout.close()
        with self.assertRaises(store.Busy):
            ingest.run_pass(self.home, self.roots, fullfsync=False)
        holder.kill()
        holder.wait()
        stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(stats.events_added, 2)

    def test_crash_mid_source_rolls_back(self):
        self.write(rollout("0000-bad"), primary("0000-bad"))
        self.write(rollout(), primary())
        real = ingest._commit_source

        def boom(conn, src_id, *args, **kw):
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


def fc_output(ordinal, call_id, text):
    payload = {
        "type": "function_call_output",
        "call_id": call_id,
        "id": "o",
        "output": text,
        "internal_chat_message_metadata_passthrough": {},
    }
    return {
        "timestamp": "t",
        "type": "response_item",
        "ordinal": ordinal,
        "payload": payload,
    }


FAILED = "Chunk ID: a\nWall time: 0.1 seconds\nProcess exited with code {}\n"


class AmendmentTests(IngestCase):
    def default_eligible(self):
        return self.conn.execute(
            "SELECT count(*) FROM event e JOIN source s ON s.id = e.source_id"
            " WHERE s.thread_class = 'primary'"
            " AND e.kind IN ('prompt','reply','tool_call') AND e.flags & 1 = 0"
        ).fetchone()[0]

    def test_subagent_threads_indexed_non_default_class(self):
        self.write(
            rollout("thr-sub"),
            [
                subagent_meta("thr-sub", k=1),
                user_msg(1, "task"),
                reply(2, "done"),
                function_call(3, "exec_command", '{"cmd":"ls"}'),
                agent_message(
                    4, "/root", "/root/worker", "Message Type: NEW_TASK"
                ),
            ],
        )
        sub = "-work-repo/sess-1/subagents/agent-a1.jsonl"
        self.write(
            sub,
            [
                claude_rec("user", "go", sidechain=True),
                claude_rec("assistant", [text_block("ok")], sidechain=True),
            ],
            root="claude-projects",
        )
        self.run_ingest()
        kinds = self.conn.execute(
            "SELECT s.provider, s.thread_class, e.kind FROM event e"
            " JOIN source s ON s.id = e.source_id ORDER BY e.id"
        )
        self.assertEqual(
            [tuple(r) for r in kinds],
            [
                ("codex", "subagent", "delegation"),
                ("codex", "subagent", "reply"),
                ("codex", "subagent", "tool_call"),
                ("codex", "subagent", "delegation"),
                ("claude", "subagent", "delegation"),
                ("claude", "subagent", "reply"),
            ],
        )
        self.assertEqual(self.default_eligible(), 0)

    def test_reviewer_threads_row_only(self):
        guard = {"subagent": {"other": "guardian"}}
        self.write(
            rollout("thr-g1"),
            [
                codex_meta("guardian_review", "thr-g1", source=guard),
                user_msg(1, "review"),
            ],
        )
        self.write(
            rollout("thr-g2"),
            [subagent_meta("thr-g2", k=None, source=guard), user_msg(1, "r")],
        )
        self.run_ingest()
        rows = self.conn.execute(
            "SELECT thread_id, thread_class, cursor_line FROM source"
            " ORDER BY thread_id"
        )
        self.assertEqual(
            [tuple(r) for r in rows],
            [("thr-g1", "reviewer", 0), ("thr-g2", "reviewer", 0)],
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM event").fetchone()[0], 0
        )

    def test_tool_error_links_across_passes(self):
        call = function_call(1, "exec_command", '{"cmd":"make"}', "c1")
        path = self.write(rollout(), [codex_meta("user", TID), call])
        self.run_ingest()
        state = json.loads(self.source()["parse_state"])
        self.assertIn("c1", state["pending"])
        self.append(path, [fc_output(2, "c1", FAILED.format(2) + "boom")])
        self.run_ingest()  # the output arrives in a later pass (C2)
        rows = {r["kind"]: r for r in self.conn.execute("SELECT * FROM event")}
        self.assertEqual(
            rows["tool_error"]["parent_event_id"], rows["tool_call"]["id"]
        )
        self.assertEqual(
            json.loads(self.source()["parse_state"])["pending"], {}
        )  # pruned once linked
        # an exec cell continued by wait(cell_id) in yet another pass
        running = (
            "Script running with cell ID 7\nWall time 1 seconds\nOutput:\n"
        )
        self.append(
            path,
            [
                custom_call(3, "exec", "await tools.x()", "x1"),
                fc_output(4, "x1", running),
            ],
        )
        self.run_ingest()
        self.append(
            path,
            [
                function_call(5, "wait", '{"cell_id":"7"}', "w1"),
                fc_output(6, "w1", "Script failed\nOutput:\nE"),
            ],
        )
        self.run_ingest()
        exec_call = self.conn.execute(
            "SELECT id FROM event WHERE tag = 'exec' AND kind = 'tool_call'"
        ).fetchone()[0]
        linked = self.conn.execute(
            "SELECT parent_event_id FROM event WHERE line = 7"
        ).fetchone()[0]
        self.assertEqual(linked, exec_call)

    def test_event_cwd_and_commit_hint_scope(self):
        repo = self.tmp / "repo"
        git = [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e.invalid",
            "-c",
            "commit.gpgsign=false",
        ]
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        repo.mkdir()
        subprocess.run([*git, "init", "-q"], cwd=repo, env=env, check=True)
        subprocess.run(
            [*git, "commit", "-q", "--allow-empty", "-m", "i"],
            cwd=repo,
            env=env,
            check=True,
        )
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        gone = str(self.tmp / "deleted-worktree")
        meta = codex_meta(
            "user", cwd=str(repo), git={"branch": "main", "commit_hash": head}
        )
        self.write(
            rollout(),
            [
                meta,
                user_msg(1, "in repo"),
                turn_context(2, gone),
                user_msg(3, "in gone"),
            ],
        )
        self.run_ingest()
        rows = self.conn.execute(
            "SELECT e.cwd, s.key FROM event e"
            " JOIN scope s ON s.id = e.scope_id ORDER BY e.line"
        )
        self.assertEqual(
            [tuple(r) for r in rows],
            [(str(repo), str(repo)), (gone, str(repo))],
        )
        claude = [claude_rec("user", "hi", cwd=str(self.tmp / "plain"))]
        self.write("-p/sess-2.jsonl", claude, root="claude-projects")
        self.run_ingest()
        row = self.conn.execute(
            "SELECT e.cwd FROM event e JOIN source s ON s.id = e.source_id"
            " WHERE s.provider = 'claude'"
        ).fetchone()
        self.assertEqual(row["cwd"], str(self.tmp / "plain"))

    def test_usage_counts_without_storing_output(self):
        canary = "CANARY-OUTPUT-" + "7" * 12
        records = [
            codex_meta("user", TID),
            function_call(1, "exec_command", '{"cmd":"pctx search x"}', "p1"),
            fc_output(2, "p1", FAILED.format(3) + canary),
            function_call(3, "exec_command", '{"cmd":"pctx stats"}', "p2"),
            fc_output(4, "p2", FAILED.format(0) + canary),
            function_call(5, "exec_command", '{"cmd":"ls"}', "p3"),
        ]
        self.write(rollout(), records)
        claude = [
            claude_rec(
                "assistant",
                [tool_use("Bash", {"command": "pctx search y"}, "toolu_1")],
            )
        ]
        self.write("-w/sess-9.jsonl", claude, root="claude-projects")
        self.run_ingest()
        rows = self.conn.execute(
            "SELECT provider, session_root, calls, errors, last_ts"
            " FROM usage ORDER BY provider"
        )
        self.assertEqual(
            [tuple(r) for r in rows],
            [
                ("claude", "sess-1", 1, 0, claude[0]["timestamp"]),
                ("codex", TID, 2, 1, records[3]["timestamp"]),
            ],
        )
        flagged = self.conn.execute(
            "SELECT count(*) FROM event WHERE kind = 'tool_call'"
            " AND flags & 1"
        ).fetchone()[0]
        self.assertEqual(flagged, 3)  # O11
        self.conn.close()
        blob = b"".join(
            p.read_bytes() for p in self.home.iterdir() if p.is_file()
        )
        self.assertNotIn(canary.encode(), blob)  # read in memory only (O9)

    def test_parse_state_reset_on_replace(self):
        call = function_call(1, "exec_command", '{"cmd":"make"}', "c1")
        path = self.write(rollout(), [codex_meta("user", TID), call])
        self.run_ingest()
        self.assertIn(
            "c1", json.loads(self.source()["parse_state"])["pending"]
        )
        # rewrite: a different call, and an output for the OLD call id
        records = [
            codex_meta("user", TID),
            function_call(1, "exec_command", '{"cmd":"make all"}', "c2"),
            fc_output(2, "c1", FAILED.format(2)),
        ]
        self.write(rollout(), records)
        self.assertNotEqual(path.stat().st_size, 0)
        self.run_ingest()
        state = json.loads(self.source()["parse_state"])
        self.assertEqual(set(state["pending"]), {"c2"})
        kinds = [e[2] for e in self.events()]
        self.assertEqual(kinds, ["tool_call"])  # stale c1 does not link


class RootsAndPassTests(unittest.TestCase):
    def test_default_roots(self):
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
        got = ingest.default_roots({"HOME": "/h", "PCTX_ROOTS": override})
        self.assertEqual(got, {"claude-projects": Path("/h/x")})
        for bad in ('{"nope": "/x"}', "not json", '["codex-sessions"]'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ingest.default_roots({"HOME": "/h", "PCTX_ROOTS": bad})


class RaceTests(IngestCase):
    def test_file_vanishing_mid_pass_is_skipped(self):
        gone = self.write(rollout("thr-gone"), primary("thr-gone"))
        self.write(rollout(), primary())
        real = ingest._first_line

        def racing(path):
            if path == gone:
                gone.unlink()  # archived or deleted after discovery
            return real(path)

        with mock.patch.object(ingest, "_first_line", racing):
            stats = self.run_ingest()
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(len(self.events()), 2)


BASE = "0199bbbb-0000-4000-8000-000000000001"
SEG = "0199bbbb-0000-4000-8000-000000000002"


class SegmentTests(IngestCase):
    """A long Codex thread continues in new rollout files: same payload.id,
    a new rollout uuid in the name, history_base, ordinals continuing."""

    def files(self):
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

    def test_continuation_segments_are_separate_sources(self):
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

    def test_segments_follow_their_thread_for_tombstones_and_targets(self):
        self.files()
        targeted = self.run_ingest(only_threads={BASE})
        self.assertEqual(targeted.files_changed, 2)  # all of the thread
        self.conn.execute("DELETE FROM event")  # an erase, as WU7 does it
        self.conn.execute("DELETE FROM source")
        self.conn.execute(
            "INSERT INTO tombstone(created_at, provider, level, thread_id)"
            " VALUES (0, 'codex', 'thread', ?)",
            (BASE,),
        )
        self.run_ingest()
        count = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(count, 0)


class CopyTests(IngestCase):
    def test_same_pass_copies_indexed_once(self):
        self.write(rollout(), primary())
        self.write(Path(rollout()).name, primary(), root="codex-archived")
        stats = self.run_ingest()
        self.assertEqual((stats.failed, stats.skipped_files), (0, 1))
        count = self.conn.execute("SELECT count(*) FROM source").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(len(self.events()), 2)
