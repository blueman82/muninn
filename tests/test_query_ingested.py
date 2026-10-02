"""Query contract against what the real ingest stored."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pctx import ingest, query, store
from tests.test_classify import (
    CWD,
    SESSION,
    agent_message,
    claude_rec,
    codex_meta,
    function_call,
    reply,
    subagent_meta,
    user_msg,
)
from tests.test_ingest import line as jsonl_line
from tests.test_ingest import rollout

PARENT_TID = "0199aaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SUB_TID = "0199aaaa-bbbb-4ccc-8ddd-111111111111"
GUARD_TID = "0199aaaa-bbbb-4ccc-8ddd-222222222222"


class IngestedTests(unittest.TestCase):
    """Search, open and sessions against rows the real ingest wrote.

    Each canary word marks one kind of record so a hit shows which records
    recall returns by default and which it keeps back.
    """

    tmp: Path
    roots: dict[str, Path]
    db: Path
    rw: sqlite3.Connection
    ro: sqlite3.Connection
    parent_records: list[dict[str, Any]]
    parent: Path

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.roots = {
            "codex-sessions": self.tmp / "codex" / "sessions",
            "codex-archived": self.tmp / "codex" / "archived_sessions",
            "claude-projects": self.tmp / "claude" / "projects",
        }
        for path in self.roots.values():
            path.mkdir(parents=True)
        self.db = store.db_path(self.tmp / "home")
        self.rw = store.connect_rw(self.db, fullfsync=False)
        self.addCleanup(self.rw.close)
        self.parent_records = [
            codex_meta("user", PARENT_TID),
            user_msg(1, "remember the CANARYB build flag is off"),
            reply(2, "noted CANARYB"),
            function_call(3, "shell", '{"cmd": "echo CANARYT"}'),
            agent_message(4, "/root/worker", "/root", "report CANARYR"),
        ]
        self.parent = self.write(rollout(PARENT_TID), self.parent_records)
        self.write(
            rollout(GUARD_TID),
            [
                codex_meta(
                    "guardian_review",
                    GUARD_TID,
                    session_id=PARENT_TID,
                    source={"subagent": {"other": "guardian"}},
                ),
                user_msg(1, "review: CANARYA approve?"),
            ],
        )
        self.write(
            rollout(SUB_TID),
            [
                subagent_meta(
                    SUB_TID,
                    k=3,
                    session_id=PARENT_TID,
                    parent_thread_id=PARENT_TID,
                    forked_from_id=PARENT_TID,
                ),
                codex_meta("user", PARENT_TID, ordinal=1),
                user_msg(2, "remember the CANARYB build flag is off"),
                user_msg(3, "the task: CANARYC"),
                reply(4, "done CANARYC"),
            ],
        )
        pasted = '<pctx-memory source="pctx" trust="untrusted-data">CANARYD'
        self.write(
            f"-work-repo/{SESSION}.jsonl",
            [
                claude_rec("user", f"see this {pasted}</pctx-memory>"),
                claude_rec("user", "CANARYE from the claude session"),
            ],
            root="claude-projects",
        )
        ingest.ingest(self.rw, self.roots)
        self.ro = store.connect_ro(self.db)
        self.addCleanup(self.ro.close)

    def write(
        self,
        rel: str,
        records: list[dict[str, Any]],
        root: str = "codex-sessions",
    ) -> Path:
        """Write a transcript file under one of the temp roots.

        Args:
            rel: Path of the file relative to the root.
            records: The JSON records, one per line.
            root: Name of the root to write under.

        Returns:
            The path written.
        """
        path = self.roots[root] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(map(jsonl_line, records)))
        return path

    def find(self, text: str, **kw: Any) -> dict[str, Any]:
        """Search as an anonymous caller in the fixture's working directory.

        Args:
            text: The search text.
            **kw: Extra ``query.search`` keywords; ``env`` defaults to empty.

        Returns:
            The search answer.
        """
        kw.setdefault("env", {})
        return query.search(self.ro, text, cwd=CWD, **kw)

    def refs(self, text: str, **kw: Any) -> list[str]:
        """Return the refs of the hits for a search.

        Args:
            text: The search text.
            **kw: Extra ``query.search`` keywords.

        Returns:
            One ref per hit, in rank order.
        """
        return [h["ref"] for h in self.find(text, **kw)["hits"]]

    def test_canaries_are_found_and_opened_byte_exact_with_neighbours(
        self,
    ) -> None:
        hits = self.find("CANARYB")["hits"]
        (hit,) = [h for h in hits if h["kind"] == "prompt"]
        self.assertEqual(hit["provider"], "codex")
        self.assertTrue(hit["ref"].startswith(f"codex:{PARENT_TID}:"))
        got = query.open_event(
            self.ro, hit["ref"], roots=self.roots, raw=True, context=2
        )
        lines = self.parent.read_bytes().split(b"\n")
        number = got["provenance"]["line"]
        self.assertEqual(got["raw"].encode(), lines[number - 1])
        digest = hashlib.sha256(got["raw"].encode()).hexdigest()
        self.assertEqual(digest, got["provenance"]["line_sha256"])
        self.assertTrue(got["hash_ok"])
        self.assertEqual(got["text"], "remember the CANARYB build flag is off")
        after = [n for n in got["neighbours"] if n["rel"] > 0]
        self.assertEqual([n["kind"] for n in after], ["reply", "tool_call"])
        claude = self.find("CANARYE")["hits"][0]
        self.assertEqual(claude["provider"], "claude")
        opened = query.open_event(
            self.ro, claude["ref"], roots=self.roots, raw=True
        )
        self.assertTrue(opened["hash_ok"])
        self.assertIn("CANARYE", opened["raw"])

    def test_default_recall_leaves_out_reviewer_replay_echo_and_current(
        self,
    ) -> None:
        for opted in (False, True):  # the prompt and the reply, never a replay
            refs = self.refs("CANARYB", include_subagents=opted)
            self.assertEqual(len(refs), 2)
            for ref in refs:
                self.assertTrue(ref.startswith(f"codex:{PARENT_TID}:"))
        for opted in (False, True):
            self.assertEqual(self.refs("CANARYA", include_subagents=opted), [])
            self.assertEqual(self.refs("CANARYD", include_subagents=opted), [])
        self.assertEqual(self.refs("CANARYC"), [])
        wide = self.find("CANARYC", include_subagents=True)["hits"]
        self.assertEqual(
            sorted((h["kind"], h["class"]) for h in wide),
            [("delegation", "subagent"), ("reply", "subagent")],
        )
        self.assertEqual(self.refs("CANARYR"), [])
        (report,) = self.find("CANARYR", include_subagents=True)["hits"]
        self.assertEqual(
            (report["kind"], report["tag"]), ("harness", "agent_message")
        )
        mine = {"CLAUDE_CODE_SESSION_ID": SESSION}
        self.assertEqual(self.refs("CANARYE", env=mine), [])
        self.assertEqual(
            len(self.refs("CANARYE", env=mine, include_current=True)), 1
        )
        self.assertEqual(len(self.refs("CANARYE", current_session="other")), 1)

    def test_sessions_and_appended_records_keep_identities(self) -> None:
        before = self.refs("CANARYB")
        listing = query.sessions(self.ro, cwd=CWD)
        by_root = {s["session"]: s for s in listing["sessions"]}
        parent = by_root[PARENT_TID]
        self.assertEqual(parent["threads"], 3)  # parent, subagent, reviewer
        self.assertEqual(parent["forks"], 1)
        self.assertEqual(parent["kinds"]["prompt"], 1)
        self.assertIn(SESSION, by_root)
        with self.parent.open("ab") as handle:
            handle.write(jsonl_line(user_msg(5, "later note CANARYF")))
        ingest.ingest(self.rw, self.roots)
        self.assertEqual(self.refs("CANARYB"), before)
        self.assertEqual(len(self.refs("CANARYF")), 1)
        timeline = query.session(self.ro, PARENT_TID)["events"]
        self.assertEqual(timeline[-1]["ref"], self.refs("CANARYF")[0])
        mine = [e["ref"] for e in timeline if f":{PARENT_TID}:" in e["ref"]]
        numbers = [int(r.rsplit(":", 1)[1].split(".")[0]) for r in mine]
        self.assertEqual(numbers, sorted(numbers))  # source order in a thread
