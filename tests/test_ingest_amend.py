"""Ingest of non-primary threads, tool errors, scopes and usage counters."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from tests.ingest_support import (
    FAILED,
    TID,
    IngestCase,
    fc_output,
    rollout,
)
from tests.test_classify import (
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


class AmendmentTests(IngestCase):
    """Thread classes, tool-error linking, scope hints and usage counts."""

    def default_eligible(self) -> int:
        """Count the events that default search would return.

        Returns:
            The number of unflagged prompt, reply and tool_call events that
            belong to primary threads.
        """
        count: int = self.conn.execute(
            "SELECT count(*) FROM event e JOIN source s ON s.id = e.source_id"
            " WHERE s.thread_class = 'primary'"
            " AND e.kind IN ('prompt','reply','tool_call') AND e.flags & 1 = 0"
        ).fetchone()[0]
        return count

    def test_subagent_threads_indexed_non_default_class(self) -> None:
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

    def test_reviewer_threads_row_only(self) -> None:
        guard = {"subagent": {"other": "guardian"}}
        # k=None omits the replay start ordinal; the helper types k as int.
        no_start: dict[str, Any] = {"k": None}
        self.write(
            rollout("thr-g1"),
            [
                codex_meta("guardian_review", "thr-g1", source=guard),
                user_msg(1, "review"),
            ],
        )
        self.write(
            rollout("thr-g2"),
            [
                subagent_meta("thr-g2", source=guard, **no_start),
                user_msg(1, "r"),
            ],
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

    def test_tool_error_links_across_passes(self) -> None:
        call = function_call(1, "exec_command", '{"cmd":"make"}', "c1")
        path = self.write(rollout(), [codex_meta("user", TID), call])
        self.run_ingest()
        state = json.loads(self.need_source()["parse_state"])
        self.assertIn("c1", state["pending"])
        self.append(path, [fc_output(2, "c1", FAILED.format(2) + "boom")])
        self.run_ingest()  # the output arrives in a later pass
        rows = {r["kind"]: r for r in self.conn.execute("SELECT * FROM event")}
        self.assertEqual(
            rows["tool_error"]["parent_event_id"], rows["tool_call"]["id"]
        )
        self.assertEqual(
            json.loads(self.need_source()["parse_state"])["pending"], {}
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

    def test_event_cwd_and_commit_hint_scope(self) -> None:
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
        # The deleted worktree still maps to its repo through the commit hash.
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

    def test_usage_counts_without_storing_output(self) -> None:
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
        self.assertEqual(flagged, 3)  # tool calls are flagged, not searchable
        self.conn.close()
        blob = b"".join(
            p.read_bytes() for p in self.home.iterdir() if p.is_file()
        )
        # Output is parsed in memory only; no stored byte may contain it.
        self.assertNotIn(canary.encode(), blob)

    def test_parse_state_reset_on_replace(self) -> None:
        call = function_call(1, "exec_command", '{"cmd":"make"}', "c1")
        path = self.write(rollout(), [codex_meta("user", TID), call])
        self.run_ingest()
        self.assertIn(
            "c1", json.loads(self.need_source()["parse_state"])["pending"]
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
        state = json.loads(self.need_source()["parse_state"])
        self.assertEqual(set(state["pending"]), {"c2"})
        kinds = [e[2] for e in self.events()]
        self.assertEqual(kinds, ["tool_call"])  # stale c1 does not link
