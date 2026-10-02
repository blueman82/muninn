"""Erase contract: tombstone files, scope reporting and residue filters.

Synthetic provider trees and a temp MUNINN_HOME only (IngestCase); knowledge
rows are inserted directly.  Canaries are synthetic.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from muninn import erase, store
from tests.erase_support import CANARY, EraseCase, digest
from tests.test_classify import codex_meta, user_msg
from tests.test_ingest import HOLD_LOCK, ROOT, TID, primary, rollout


class TombstoneFileTests(EraseCase):
    """Tombstones hold no text and survive a rebuild."""

    def ingest_canaries(self) -> None:
        """Ingest two sessions that both contain the canary."""
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

    def test_tombstone_stores_no_text(self) -> None:
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

    def test_tombstones_jsonl_written_and_reapplied(self) -> None:
        self.ingest_canaries()
        self.erase(session=TID)
        self.erase(match=CANARY)
        path = self.home / "tombstones.jsonl"
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
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

    def test_provider_files_untouched(self) -> None:
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
    """What the erase reports as out of scope and its locking."""

    def test_out_of_scope_listing(self) -> None:
        home = Path(self.env["HOME"])
        present = [
            home / ".codex/plugins/cache/muninn-local",
            home / ".codex/memories",
        ]
        for path in present:
            path.mkdir(parents=True)
        out = self.erase(session=TID, dry_run=True)
        self.assertEqual(out["out_of_scope"], sorted(map(str, present)))
        for path in present:
            self.assertTrue(path.is_dir())  # listed, never deleted
        not_covered = out["not_covered"]
        assert isinstance(not_covered, list)
        for note in ("Time Machine", "free filesystem blocks"):
            self.assertTrue(any(note in n for n in not_covered))

    def test_run_erase_takes_the_writer_lock(self) -> None:
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
        stdout = holder.stdout
        assert stdout is not None
        self.assertEqual(stdout.readline().strip(), "ready")
        stdout.close()
        with self.assertRaises(store.BusyError):
            erase.run_erase(self.home, session=TID, env=self.env, wait_s=0)
        holder.kill()
        holder.wait()
        out = erase.run_erase(self.home, session=TID, env=self.env)
        self.assertEqual(out["events"], 2)
        self.assertEqual(len(self.events()), 0)


class ResidueFilterTests(EraseCase):
    """Residue accounting when text survives elsewhere."""

    def test_text_surviving_in_another_session_is_not_residue(self) -> None:
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

    def test_session_erase_scrubs_citing_knowledge(self) -> None:
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
    """Event reference parsing."""

    def test_event_ref_uses_the_query_ref_format(self) -> None:
        self.write(rollout(), primary())
        self.run_ingest()
        out = self.erase(event_ref=f"codex:{TID}:2")  # part defaults to 1
        self.assertEqual((out["lines"], out["events"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
