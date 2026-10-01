"""Erase contract (design 4.3, 3.8, 5; spec O4d, O10, O13, A13).

Synthetic provider trees and a temp PCTX_HOME only (IngestCase); knowledge
rows are inserted directly.  Canaries are synthetic.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from pctx import erase, store
from tests.test_classify import codex_meta, reply, subagent_meta, user_msg
from tests.test_ingest import (
    BASE,
    HOLD_LOCK,
    ROOT,
    SEG,
    TID,
    IngestCase,
    primary,
    rollout,
)

CANARY = "CANARY-ERASE-" + "q7" * 10  # 33 chars, one token


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EraseCase(IngestCase):
    def setUp(self):
        super().setUp()
        self.env = {
            "HOME": str(self.tmp / "userhome"),
            "PCTX_ROOTS": json.dumps(
                {k: str(v) for k, v in self.roots.items()}
            ),
        }

    def erase(self, **kw):
        return erase.erase(self.conn, home=self.home, env=self.env, **kw)

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def knowledge(self, text, cites):
        """A current entry citing (thread_id, line, part, quote) events."""
        sid = self.count("SELECT id FROM scope LIMIT 1")
        kid = self.conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', ?, 'current', 'user', 0)",
            (sid, text),
        ).lastrowid
        for thread, line, part, quote in cites:
            self.conn.execute(
                "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
                " part, line_sha256, role, kind, quote, span_start, span_end)"
                " VALUES (?, 'codex', ?, ?, ?, 'h', 'user', 'prompt', ?,"
                " 0, 1)",
                (kid, thread, line, part, quote),
            )
        return kid


class SessionEraseTests(EraseCase):
    def test_erase_session_then_rescan_absent(self):
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
        os.rename(path, self.roots["codex-archived"] / path.name)
        self.run_ingest()  # an archive move cannot resurrect it
        self.assertEqual(
            self.count("SELECT count(*) FROM source WHERE thread_id = ?", TID),
            0,
        )

    def test_erase_session_covers_continuation_files(self):
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

    def test_forks_of_erased_session_erased(self):
        self.write(rollout(TID), primary())
        fork = codex_meta("user", "thr-fork", forked_from_id=TID)
        self.write(rollout("thr-fork"), [fork, user_msg(1, "fork own")])
        sub = subagent_meta(
            "thr-fsub",
            k=None,
            session_id="thr-fork",
            parent_thread_id="thr-fork",
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

    def test_unknown_session_is_tombstoned_for_later_ingest(self):
        out = self.erase(session=TID)
        self.assertEqual(out["sources"], 0)
        self.write(rollout(), primary())
        self.run_ingest()  # arrives after the erase: never indexed
        self.assertEqual(self.events(), [])


class EventAndMatchTests(EraseCase):
    def test_erase_event_line_tombstone(self):
        records = primary() + [user_msg(3, "q two")]
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
            with self.subTest(ref=bad):
                with self.assertRaises((LookupError, ValueError)):
                    self.erase(event_ref=bad)

    def test_erase_match_scrubs_knowledge_and_quotes(self):
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

    def test_match_needs_a_real_string_and_one_target(self):
        for kw in (
            {},
            {"match": ""},
            {"match": "  ab "},
            {"session": TID, "match": CANARY},
        ):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    self.erase(**kw)


class VerificationTests(EraseCase):
    def test_residue_scan_zero_over_home(self):
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
        journal = self.home / "pctx.sqlite-journal"  # a journal is scanned
        journal.write_bytes(b"page..." + CANARY.encode())
        self.assertEqual(
            erase.residue_scan(self.home, [CANARY.encode()]),
            ["pctx.sqlite-journal"],
        )

    def test_vocab_check_passes_after_erase_and_fails_on_unsecured_delete(
        self,
    ):
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
        rare = erase._rare_terms(self.conn, ids, [])
        self.assertTrue(rare["event_fts"])
        self.conn.execute("DROP TRIGGER event_ad")
        self.conn.execute("DELETE FROM event")
        self.assertEqual(
            erase._vocab_left(self.conn, rare), len(rare["event_fts"])
        )

    def test_dry_run_changes_nothing(self):
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


class TombstoneFileTests(EraseCase):
    def ingest_canaries(self):
        self.write(
            rollout(),
            [
                codex_meta("user", TID),
                user_msg(1, f"{CANARY} one"),
                user_msg(2, f"{CANARY} two"),
            ],
        )
        self.write(
            rollout("thr-m"),
            [codex_meta("user", "thr-m"), user_msg(1, f"see {CANARY}")],
        )
        self.run_ingest()

    def test_tombstone_stores_no_text(self):
        self.ingest_canaries()
        self.erase(event_ref=f"codex:{TID}:2.1")
        self.erase(match=CANARY)
        self.erase(session="thr-m")
        dumped = json.dumps(
            [tuple(r) for r in self.conn.execute("SELECT * FROM tombstone")]
        )
        logs = json.dumps(
            [
                tuple(r)
                for r in self.conn.execute("SELECT * FROM knowledge_log")
            ]
        )
        journal = (self.home / "tombstones.jsonl").read_text()
        for blob in (dumped, logs, journal):
            self.assertNotIn(CANARY, blob)
            self.assertNotIn("one", blob)
        levels = sorted(
            r[0] for r in self.conn.execute("SELECT level FROM tombstone")
        )
        self.assertEqual(levels, ["line", "line", "line", "session"])

    def test_tombstones_jsonl_written_and_reapplied(self):
        self.ingest_canaries()
        self.erase(session=TID)
        self.erase(match=CANARY)
        path = self.home / "tombstones.jsonl"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            len(rows), self.count("SELECT count(*) FROM tombstone")
        )
        self.assertEqual(erase.reapply_tombstones(self.conn, self.home), 0)
        self.conn.execute("DELETE FROM tombstone")  # e.g. a rebuild
        restored = erase.reapply_tombstones(self.conn, self.home)
        self.assertEqual(restored, len(rows))
        self.run_ingest(full=True)
        self.assertEqual(self.count("SELECT count(*) FROM event"), 0)

    def test_provider_files_untouched(self):
        self.ingest_canaries()
        files = sorted(
            p for r in self.roots.values() for p in r.rglob("*") if p.is_file()
        )
        before = {p: digest(p) for p in files}
        out = self.erase(session=TID)
        self.erase(match=CANARY)
        self.assertEqual({p: digest(p) for p in files}, before)
        self.assertEqual(
            out["provider_files"],
            [str(self.roots["codex-sessions"] / rollout())],
        )


class ScopeOfEraseTests(EraseCase):
    def test_out_of_scope_listing(self):
        home = Path(self.env["HOME"])
        present = [
            home / ".codex/plugins/cache/provenance-context-local",
            home / ".codex/memories",
        ]
        for path in present:
            path.mkdir(parents=True)
        out = self.erase(session=TID, dry_run=True)
        self.assertEqual(out["out_of_scope"], sorted(map(str, present)))
        for path in present:
            self.assertTrue(path.is_dir())  # listed, never deleted
        for note in ("Time Machine", "free filesystem blocks"):
            self.assertTrue(any(note in n for n in out["not_covered"]))

    def test_run_erase_takes_the_writer_lock(self):
        self.write(rollout(), primary())
        self.run_ingest()
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
            erase.run_erase(self.home, session=TID, env=self.env, wait_s=0)
        holder.kill()
        holder.wait()
        out = erase.run_erase(self.home, session=TID, env=self.env)
        self.assertEqual(out["events"], 2)
        self.assertEqual(len(self.events()), 0)


class ResidueFilterTests(EraseCase):
    def test_text_surviving_in_another_session_is_not_residue(self):
        same = f"{CANARY} repeated in two sessions"
        self.write(rollout(), [codex_meta("user", TID), user_msg(1, same)])
        self.write(
            rollout("thr-dup"),
            [codex_meta("user", "thr-dup"), user_msg(1, same)],
        )
        self.run_ingest()
        out = self.erase(session=TID)
        self.assertEqual((out["residue"], out["needles"]), (0, 0))
        self.assertEqual(len(self.events("thr-dup")), 1)

    def test_session_erase_scrubs_citing_knowledge(self):
        self.write(rollout(), primary())
        self.write(rollout("thr-keep"), primary("thr-keep"))
        self.run_ingest()
        gone = self.knowledge("only from the erased", [(TID, 2, 1, "q one")])
        kept = self.knowledge(
            "two sources", [(TID, 3, 1, "a one"), ("thr-keep", 2, 1, "q one")]
        )
        out = self.erase(session=TID)
        status = dict(self.conn.execute("SELECT id, status FROM knowledge"))
        self.assertEqual((status[gone], status[kept]), ("erased", "current"))
        self.assertEqual((out["knowledge"], out["citations"]), (1, 2))
        quotes = [
            r[0]
            for r in self.conn.execute(
                "SELECT quote FROM citation WHERE state = 'live'"
            )
        ]
        self.assertEqual(quotes, ["q one"])  # the surviving session's quote


class RefFormatTests(EraseCase):
    def test_event_ref_uses_the_query_ref_format(self):
        self.write(rollout(), primary())
        self.run_ingest()
        out = self.erase(event_ref=f"codex:{TID}:2")  # part defaults to 1
        self.assertEqual((out["lines"], out["events"]), (1, 1))
