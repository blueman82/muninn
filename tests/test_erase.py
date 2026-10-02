"""Erase contract: sessions, event lines, text matches and verification.

Synthetic provider trees and a temp MUNINN_HOME only (IngestCase); knowledge
rows are inserted directly.  Canaries are synthetic.
"""

from __future__ import annotations

import pathlib
import unittest
from typing import Any

from muninn import erase, erase_residue, store
from tests.erase_support import CANARY, EraseCase, digest
from tests.test_classify import codex_meta, reply, subagent_meta, user_msg
from tests.test_ingest import BASE, SEG, TID, primary, rollout


class SessionEraseTests(EraseCase):
    """Erasing a whole session and what a rescan may restore."""

    def test_erase_session_then_rescan_absent(self) -> None:
        path = self.write(rollout(), primary())
        self.write(rollout("thr-keep"), primary("thr-keep"))
        self.run_ingest()
        out = self.erase(session=TID)
        self.assertEqual((out["sources"], out["events"]), (1, 2))
        self.assertEqual(
            self.count("SELECT count(*) FROM source WHERE thread_id = ?", TID),
            0,
        )
        self.run_ingest(full=True)  # rescan: the tombstone holds
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.events("thr-keep")), 2)
        pathlib.Path(path).rename(self.roots["codex-archived"] / path.name)
        self.run_ingest()  # an archive move cannot resurrect it
        self.assertEqual(
            self.count("SELECT count(*) FROM source WHERE thread_id = ?", TID),
            0,
        )

    def test_erase_session_covers_continuation_files(self) -> None:
        self.write(rollout(BASE), [codex_meta("user", BASE), user_msg(1, "a")])
        hb = {
            "end_byte_offset": 1,
            "end_ordinal_exclusive": 2,
            "thread_id": BASE,
        }
        seg = codex_meta("user", BASE, ordinal=2, history_base=hb)
        self.write(rollout(SEG), [seg, user_msg(3, "b")])
        self.run_ingest()
        out = self.erase(session=BASE)
        self.assertEqual(out["sources"], 2)
        self.run_ingest(full=True)
        self.assertEqual(self.count("SELECT count(*) FROM source"), 0)

    def test_forks_of_erased_session_erased(self) -> None:
        self.write(rollout(TID), primary())
        fork = codex_meta("user", "thr-fork", forked_from_id=TID)
        self.write(rollout("thr-fork"), [fork, user_msg(1, "fork own")])
        # subagent_meta skips the history-start ordinal when k is None;
        # its unannotated k=5 default makes a literal k=None fail type
        # checking, so the override goes through a dict.
        no_history: dict[str, Any] = {"k": None}
        sub = subagent_meta(
            "thr-fsub",
            session_id="thr-fork",
            parent_thread_id="thr-fork",
            **no_history,
        )
        self.write(rollout("thr-fsub"), [sub, user_msg(1, "sub task")])
        self.write(rollout("thr-other"), primary("thr-other"))
        self.run_ingest()
        out = self.erase(session=TID)
        self.assertEqual((out["sessions"], out["sources"]), (2, 3))
        self.run_ingest(full=True)
        left = [
            r[0] for r in self.conn.execute("SELECT thread_id FROM source")
        ]
        self.assertEqual(left, ["thr-other"])

    def test_unknown_session_is_tombstoned_for_later_ingest(self) -> None:
        out = self.erase(session=TID)
        self.assertEqual(out["sources"], 0)
        self.write(rollout(), primary())
        self.run_ingest()  # arrives after the erase: never indexed
        self.assertEqual(self.events(), [])


class EventAndMatchTests(EraseCase):
    """Erasing one event line or every text that matches."""

    def test_erase_event_line_tombstone(self) -> None:
        records = [*primary(), user_msg(3, "q two")]
        self.write(rollout(), records)
        self.run_ingest()
        out = self.erase(event_ref=f"codex:{TID[:13]}:2.1")  # id prefix ok
        self.assertEqual((out["lines"], out["events"]), (1, 1))
        self.assertEqual([e[3] for e in self.events()], ["a one", "q two"])
        row = self.conn.execute(
            "SELECT level, thread_id, line, line_sha256 FROM tombstone"
        ).fetchone()
        self.assertEqual(tuple(row)[:3], ("line", TID, 2))
        self.run_ingest(full=True)  # a replace skips only that line
        self.assertEqual([e[3] for e in self.events()], ["a one", "q two"])
        for bad in ("codex:nope:2.1", f"codex:{TID}:9.1", "garbage"):
            with (
                self.subTest(ref=bad),
                self.assertRaises((LookupError, ValueError)),
            ):
                self.erase(event_ref=bad)

    def test_erase_match_scrubs_knowledge_and_quotes(self) -> None:
        self.write(
            rollout(),
            [
                codex_meta("user", TID),
                user_msg(1, f"my key is {CANARY} ok"),
                reply(2, "fine"),
                user_msg(3, "unrelated"),
            ],
        )
        self.run_ingest()
        by_text = self.knowledge(
            f"remember {CANARY}", [(TID, 4, 1, "unrelated")]
        )
        by_quote = self.knowledge("a fact", [(TID, 3, 1, f"x {CANARY} y")])
        by_event = self.knowledge("cites it", [(TID, 2, 1, "my key is")])
        mixed = self.knowledge(
            "kept", [(TID, 2, 1, "my key"), (TID, 3, 1, "fine")]
        )
        out = self.erase(match=CANARY)
        self.assertEqual(out["events"], 1)
        status = dict(self.conn.execute("SELECT id, status FROM knowledge"))
        self.assertEqual(
            [status[k] for k in (by_text, by_quote, by_event, mixed)],
            ["erased", "erased", "erased", "current"],
        )
        texts = [
            r[0]
            for r in self.conn.execute(
                "SELECT text FROM knowledge WHERE status = 'erased'"
            )
        ]
        self.assertEqual(texts, [None, None, None])
        states = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT state, quote IS NULL FROM citation ORDER BY id"
            )
        ]
        self.assertEqual(
            states,
            [
                ("live", 0),
                ("erased", 1),
                ("erased", 1),
                ("erased", 1),
                ("live", 0),
            ],
        )
        logs = self.count(
            "SELECT count(*) FROM knowledge_log WHERE action = 'erase'"
        )
        self.assertEqual(logs, 3)
        self.assertEqual(out["knowledge"], 3)

    def test_match_needs_a_real_string_and_one_target(self) -> None:
        for session, match in (
            (None, None),
            (None, ""),
            (None, "  ab "),
            (TID, CANARY),
        ):
            with (
                self.subTest(session=session, match=match),
                self.assertRaises(ValueError),
            ):
                self.erase(session=session, match=match)


class VerificationTests(EraseCase):
    """Residue, vocabulary and dry-run guarantees after an erase."""

    def test_residue_scan_zero_over_home(self) -> None:
        self.write(
            rollout(),
            [codex_meta("user", TID), user_msg(1, f"{CANARY} in a prompt")],
        )
        self.run_ingest()
        out = self.erase(session=TID)
        self.assertEqual(out["residue"], 0)
        self.conn.close()
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), path)
        journal = self.home / "muninn.sqlite-journal"  # a journal is scanned
        journal.write_bytes(b"page..." + CANARY.encode())
        self.assertEqual(
            erase.residue_scan(self.home, [CANARY.encode()]),
            ["muninn.sqlite-journal"],
        )

    def test_vocab_check_passes_after_erase_and_fails_on_unsecured_delete(
        self,
    ) -> None:
        self.write(
            rollout(), [codex_meta("user", TID), user_msg(1, f"{CANARY} rare")]
        )
        self.write(
            rollout("thr-b"),
            [codex_meta("user", "thr-b"), user_msg(1, f"{CANARY}X other")],
        )
        self.run_ingest()
        out = self.erase(session=TID)
        self.assertEqual(out["vocab"], 0)
        # negative control: a delete that bypasses the FTS triggers
        ids = [r[0] for r in self.conn.execute("SELECT id FROM event")]
        rare = erase_residue.rare_terms(self.conn, ids, [])
        self.assertTrue(rare["event_fts"])
        self.conn.execute("DROP TRIGGER event_ad")
        self.conn.execute("DELETE FROM event")
        self.assertEqual(
            erase_residue.vocab_left(self.conn, rare), len(rare["event_fts"])
        )

    def test_dry_run_changes_nothing(self) -> None:
        self.write(rollout(), primary())
        self.run_ingest()
        before = digest(store.db_path(self.home))
        files = sorted(p.name for p in self.home.iterdir())
        out = self.erase(session=TID, dry_run=True)
        self.assertEqual(
            (out["dry_run"], out["sources"], out["events"]), (True, 1, 2)
        )
        self.assertEqual(digest(store.db_path(self.home)), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), files)
        self.assertEqual(len(self.events()), 2)


if __name__ == "__main__":
    unittest.main()
