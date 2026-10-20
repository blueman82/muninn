"""Persisted family coverage protects copied prefixes and later own turns."""

from __future__ import annotations

import json
from argparse import Namespace
from contextlib import closing

from muninn import cli_rebuild, store, tombstones
from tests.test_classify import (
    claude_rec,
    codex_meta,
    reply,
    text_block,
    tool_use,
    user_msg,
)
from tests.test_ingest import rollout
from tests.tombstone_replay_support import CONTROL, TARGET, ReplayCase

FORK = "replay-fork"
TEXT = "familycopiedsecret"


class TombstoneFamilyTests(ReplayCase):
    """Exercise real plans, then retain or damage synthetic intent."""

    def family(self, *, sibling: bool = False) -> None:
        """Store a copied suffix before its ancestor grows to contain it."""
        parent = self.write(
            rollout(TARGET),
            [codex_meta("user", TARGET), user_msg(1, "parent start")],
        )
        self.run_ingest()
        for thread in (FORK, "replay-sibling") if sibling else (FORK,):
            self.write(
                rollout(thread),
                [
                    codex_meta("user", thread, forked_from_id=TARGET),
                    user_msg(1, "parent start"),
                    reply(2, TEXT),
                    user_msg(3, "fork own boundary"),
                    reply(4, TEXT),
                ],
            )
        self.session(CONTROL, TEXT)
        self.run_ingest()
        self.append(parent, [reply(2, TEXT)])
        self.run_ingest()
        self.assertEqual(
            [r[3] for r in self.events(FORK)],
            [TEXT, "fork own boundary", TEXT],
        )

    def damage(self, *, level: str) -> None:
        """Remove a whole known family record to expose uncovered history."""
        path = self.home / "tombstones.jsonl"
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows = [
            r
            for r in rows
            if not (
                r["level"] == level
                and (
                    r.get("thread_id") == FORK or r.get("session_root") == FORK
                )
                and (level != "line" or r["line"] > 0)
            )
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_positive_family_reopen_second_replay_preserves_later_turn(
        self,
    ) -> None:
        self.family()
        journal = self.interrupt("event", f"codex:{TARGET}:3.1")
        self.assertGreater(self.replay(), 0)
        self.assertEqual(
            [r[3] for r in self.events(FORK)], ["fork own boundary", TEXT]
        )
        self.assertEqual([r[3] for r in self.events(CONTROL)], [TEXT])
        self.conn.close()
        fresh = self.child("reopen", str(self.home))
        self.assertEqual((fresh.returncode, fresh.stderr), (0, ""))
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(store.connect_rw(store.db_path(self.home))) as conn,
        ):
            self.assertEqual(tombstones.reapply_tombstones(conn, self.home), 0)
            tombstones.require_current_tombstones(conn, self.home)
            self.assertEqual(
                [
                    r[0]
                    for r in conn.execute(
                        "SELECT e.text FROM event e JOIN source s "
                        "ON s.id=e.source_id "
                        "WHERE s.thread_id=? ORDER BY e.line,e.part",
                        (FORK,),
                    )
                ],
                ["fork own boundary", TEXT],
            )
        self.assertEqual(
            (self.home / "tombstones.jsonl").read_bytes(), journal
        )

    def test_uncovered_family_copy_refuses_before_any_deletion(self) -> None:
        self.family()
        self.interrupt("event", f"codex:{TARGET}:3.1")
        self.damage(level="line")
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)

    def test_uncovered_session_descendant_refuses(self) -> None:
        self.family()
        self.interrupt()
        self.damage(level="session")
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(self.count("tombstone"), 0)

    def test_missing_ancestor_and_uncovered_copy_refuses(self) -> None:
        self.family()
        self.interrupt("event", f"codex:{TARGET}:3.1")
        self.damage(level="line")
        self.conn.execute(
            "DELETE FROM event WHERE source_id IN "
            "(SELECT id FROM source WHERE thread_id=?)",
            (TARGET,),
        )
        self.conn.execute("DELETE FROM source WHERE thread_id=?", (TARGET,))
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_changed_hash_at_recorded_fork_coordinate_survives(self) -> None:
        self.family()
        self.interrupt("event", f"codex:{TARGET}:3.1")
        event = dict(
            self.conn.execute(
                "SELECT e.* FROM event e JOIN source s ON s.id=e.source_id "
                "WHERE s.thread_id=? AND e.line=3",
                (FORK,),
            ).fetchone()
        )
        self.conn.execute("DELETE FROM event WHERE id=?", (event["id"],))
        event["line_sha256"] = "f" * 64
        marks = ",".join("?" for _ in event)
        self.conn.execute(
            f"INSERT INTO event VALUES({marks})", tuple(event.values())
        )
        self.assertGreater(self.replay(), 0)
        self.assertEqual(
            [r[3] for r in self.events(FORK)],
            [TEXT, "fork own boundary", TEXT],
        )
        self.assertEqual(self.replay(), 0)

    def test_legacy_ancestor_line_hashes_need_no_content_key(self) -> None:
        self.family()
        self.interrupt("event", f"codex:{TARGET}:3.1")
        path = self.home / "tombstones.jsonl"
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows = [row for row in rows if row["line"] > 0]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        (self.home / "tombstone.key").unlink()
        self.assertEqual(self.replay(), 2)
        self.assertEqual(
            [r[3] for r in self.events(FORK)], ["fork own boundary", TEXT]
        )
        self.assertEqual(self.replay(), 0)
        self.assertFalse((self.home / "tombstone.key").exists())

    def test_thread_continuation_without_recorded_coverage_refuses(
        self,
    ) -> None:
        self.session(TARGET, "base history")
        self.session(FORK, "continued history")
        self.run_ingest()
        self.conn.execute(
            "UPDATE source SET parent_thread_id=?,session_root=? "
            "WHERE thread_id=?",
            (TARGET, TARGET, FORK),
        )
        tombstones.append_tombstones(
            self.home,
            [tombstones.tombstone_row("codex", "thread", thread_id=TARGET)],
        )
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(self.count("event"), 2)

    def test_uncovered_ancestor_copy_from_descendant_intent_refuses(
        self,
    ) -> None:
        self.family()
        self.interrupt("event", f"codex:{FORK}:3.1")
        path = self.home / "tombstones.jsonl"
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows = [r for r in rows if r["thread_id"] != TARGET]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_uncovered_sibling_copy_refuses_after_ancestor_was_scrubbed(
        self,
    ) -> None:
        self.family(sibling=True)
        self.interrupt("event", f"codex:{FORK}:3.1")
        path = self.home / "tombstones.jsonl"
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows = [r for r in rows if r["thread_id"] == FORK]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.conn.execute(
            "DELETE FROM event WHERE source_id IN "
            "(SELECT id FROM source WHERE thread_id=?) AND line=3",
            (TARGET,),
        )
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_content_tag_without_physical_coverage_refuses(self) -> None:
        self.session(TARGET, TEXT)
        self.run_ingest()
        self.interrupt("event", f"codex:{TARGET}:2.1")
        path = self.home / "tombstones.jsonl"
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows = [r for r in rows if r["line"] == 0]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_mixed_tags_need_coverage_from_all_resolving_physical_rows(
        self,
    ) -> None:
        self.session(TARGET, "own boundary", "mixedtag first", "mixedtag last")
        self.run_ingest()
        journal = self.interrupt("match", "mixedtag")
        rows = [json.loads(line) for line in journal.splitlines()]
        self.assertEqual(len(rows), 4)
        rows = [row for row in rows if row["line"] != 4]
        self.assertEqual(len(rows), 3)
        path = self.home / "tombstones.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        damaged = path.read_bytes()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(path.read_bytes(), damaged)
        self.assertFalse(self.conn.in_transaction)

    def test_one_physical_line_covers_all_parts_and_later_own_turns(
        self,
    ) -> None:
        tool = tool_use("Read", {"file_path": "/synthetic/multipart"})
        path = self.write(
            f"-synthetic/{TARGET}.jsonl",
            [
                claude_rec(
                    "assistant",
                    [text_block("multipart secret"), tool],
                    sessionId=TARGET,
                ),
                claude_rec("user", "own boundary", sessionId=TARGET),
                claude_rec("assistant", [tool], sessionId=TARGET),
            ],
            root="claude-projects",
        )
        original = path.read_bytes()
        self.run_ingest()
        self.assertEqual(self.count("event"), 4)
        journal = self.interrupt("event", f"claude:{TARGET}:1.1")
        rows = [json.loads(line) for line in journal.splitlines()]
        self.assertEqual([row["line"] for row in rows], [1, 0, 0])
        self.assertEqual(self.replay(), 3)
        self.assertEqual(
            [row[3] for row in self.events(TARGET)],
            ["own boundary", 'Read: {"file_path": "/synthetic/multipart"}'],
        )
        self.assertEqual(self.replay(), 0)
        tombstones.require_current_tombstones(self.conn, self.home)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(
            (self.home / "tombstones.jsonl").read_bytes(), journal
        )

    def test_new_uncovered_tag_refuses_after_an_older_physical_scrub(
        self,
    ) -> None:
        self.session(TARGET, "oldtag first", "newtag second")
        self.run_ingest()
        self.interrupt("event", f"codex:{TARGET}:2.1")
        self.assertEqual(self.replay(), 2)
        journal = self.interrupt("event", f"codex:{TARGET}:3.1")
        rows = [json.loads(line) for line in journal.splitlines()]
        self.assertEqual(len(rows), 4)
        rows = [row for row in rows if row["line"] != 3]
        path = self.home / "tombstones.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        damaged = path.read_bytes()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(
            ValueError, "^erasure_history_unverified$"
        ):
            self.replay()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(path.read_bytes(), damaged)
        self.assertFalse(self.conn.in_transaction)

    def test_ambiguous_history_never_reaches_rebuild_publication(self) -> None:
        self.family()
        self.interrupt("event", f"codex:{TARGET}:3.1")
        self.damage(level="line")
        self.conn.close()
        db = store.db_path(self.home)
        before, inode = db.read_bytes(), db.stat().st_ino
        code, result = cli_rebuild.rebuild(
            Namespace(), self.env, self.home, {}
        )
        assert result is not None
        self.assertEqual((code, result["reason"]), (2, "rebuild_refused"))
        self.assertFalse(result["publication_possible"])
        self.assertEqual((db.read_bytes(), db.stat().st_ino), (before, inode))
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())
        self.assertEqual(list(self.home.glob("muninn.sqlite.recovery-*")), [])
