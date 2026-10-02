"""pctx read commands: shapes, exit codes, redaction and call log."""

from __future__ import annotations

import json
from unittest import mock

from pctx import cli, store
from tests.cli_support import ALWAYS, CANARY, CliCase
from tests.test_classify import SK
from tests.test_ingest import TID, rollout
from tests.test_store import SPILLING_WRITER, Child


class ReaderTests(CliCase):
    """Read-only commands over one ingested session."""

    def setUp(self) -> None:
        super().setUp()
        self.session(TID, f"where is the {CANARY} config", "in the repo root")
        self.run_ingest()

    def test_search_shape_and_freshness(self) -> None:
        code, out, _ = self.pctx("search", CANARY)
        self.assertEqual(code, 0)
        self.assertLessEqual(
            ALWAYS | {"hits", "knowledge", "stages"}, set(out)
        )
        self.assertEqual(out["poller"], "stale")  # no heartbeat yet
        self.assertEqual(out["hits"][0]["ref"], f"codex:{TID}:2.1")
        plain = out["hits"][0]["snippet"].replace("«", "").replace("»", "")
        self.assertIn(CANARY, plain)

    def test_open_sessions_session_quote_check_shapes(self) -> None:
        ref = f"codex:{TID}:2.1"
        cases = (
            (("open", ref, "--context", "1"), {"text", "neighbours", "ref"}),
            (("open", ref, "--raw"), {"raw"}),
            (("sessions",), {"sessions"}),
            (("session", TID), {"events"}),
            (("quote-check", ref, f"the {CANARY}"), {"match", "span"}),
        )
        for argv, keys in cases:
            with self.subTest(argv=argv[0]):
                code, out, _ = self.pctx(*argv)
                self.assertEqual(code, 0, out)
                self.assertLessEqual(ALWAYS | keys, set(out))
        _, out, _ = self.pctx("quote-check", ref, f"the {CANARY}")
        self.assertIs(out["match"], True)

    def test_exit_codes(self) -> None:
        self.assertEqual(self.pctx("open", "codex:nope:1.1")[0], 2)
        self.assertEqual(self.pctx("search")[0], 2)  # usage
        code, out, _ = self.pctx(
            "search", "x", env={"PCTX_HOME": str(self.tmp / "empty")}
        )
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertIn("notice", out)
        with (
            mock.patch.object(
                cli.store,
                "connect_ro",
                side_effect=store.HotJournalError("hot"),
            ),
            mock.patch.object(
                cli.store, "heal_hot_journal", return_value=False
            ),
        ):  # sandboxed
            code, out, _ = self.pctx("search", CANARY)
        self.assertEqual((code, out["error"]), (4, "hot_journal"))

    def test_reader_heals_hot_journal_unsandboxed(self) -> None:
        child = Child(self, SPILLING_WRITER, store.db_path(self.home))
        child.wait_ready()
        child.proc.kill()  # a crashed writer leaves a hot journal
        child.proc.wait()
        journal = self.home / "pctx.sqlite-journal"
        self.assertTrue(journal.exists())
        code, out, _ = self.pctx("search", CANARY)
        self.assertEqual(code, 0, out)
        self.assertFalse(journal.exists())

    def test_output_time_redaction(self) -> None:
        secret = f"token={SK}"
        eid = self.conn.execute(
            "INSERT INTO event(source_id, line, part, byte_offset,"
            " line_sha256, seq, role, kind, scope_id, text) SELECT"
            " source_id, 99, 1, 0, 'h', 99, 'user', 'prompt', scope_id, ?"
            " FROM event LIMIT 1",
            (f"{CANARY} {secret}",),  # as if ingest had missed it
        ).lastrowid
        _, opened, _ = self.pctx("open", str(eid))
        _, found, _ = self.pctx("search", CANARY, "--limit", "30")
        # a highlight inside the secret must not defeat redaction
        _, marked, _ = self.pctx("search", f"token {CANARY}", "--limit", "30")
        dumped = json.dumps([opened, found, marked])
        self.assertNotIn(SK, dumped)
        self.assertIn("[redacted:secret]", opened["text"])
        path = self.roots["codex-sessions"] / rollout()
        with path.open("ab") as handle:  # raw line with a secret field
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "ordinal": 3,
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "clean"}
                            ],
                            "password": "hunter2x",
                        },
                    }
                ).encode()
                + b"\n"
            )
        self.bump(path)
        self.run_ingest()
        code, raw, _ = self.pctx("open", f"codex:{TID}:4.1", "--raw")
        self.assertEqual(code, 0, raw)
        self.assertNotIn("hunter2x", json.dumps(raw))
        self.assertIs(raw.get("raw_redacted"), True)

    def test_logged_flag_denied_and_disabled(self) -> None:
        _, out, _ = self.pctx("search", CANARY)
        self.assertIs(out["logged"], True)
        _, out, _ = self.pctx("search", CANARY, env={"PCTX_NO_CALLLOG": "1"})
        self.assertIs(out["logged"], False)
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        (self.home / "calls.jsonl").chmod(0o400)
        _, out, _ = self.pctx("search", CANARY)
        self.assertIs(out["logged"], False)

    def test_calls_log_has_no_text(self) -> None:
        self.pctx("search", CANARY)
        self.pctx("open", f"codex:{TID}:2.1")
        blob = (self.home / "calls.jsonl").read_text()
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("CANARY", blob.upper())
        lines = [json.loads(x) for x in blob.splitlines()]
        self.assertEqual([x["cmd"] for x in lines], ["search", "open"])
        self.assertEqual(lines[0]["n_terms"] >= 1, True)
        self.assertEqual(lines[1]["target_id"] > 0, True)
