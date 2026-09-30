"""pctx CLI contract (design 2.2, 7; spec O4c, O8d, O9, A5, A16).

In-process main() calls against synthetic provider trees in temp dirs
(IngestCase); serve runs as a subprocess.  Nothing touches the live data
dir or the real launchd domain.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest import mock

from pctx import cli, obs, store
from tests.test_classify import SK, codex_meta, reply, user_msg
from tests.test_ingest import TID, IngestCase, rollout
from tests.test_store import SPILLING_WRITER, Child

CANARY = "CANARY-CLI-" + "k4" * 9

ALWAYS = {"notice", "index_age_s", "poller", "logged"}


class CliCase(IngestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        roots = {k: str(v) for k, v in self.roots.items()}
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PCTX_", "CLAUDE_CODE_SESSION", "CODEX_"))
        }
        self.env = keep | {
            "PCTX_HOME": str(self.home),
            "PCTX_ROOTS": json.dumps(roots),
            "HOME": str(self.tmp / "userhome"),
        }

    def session(self, tid=TID, *texts):
        meta = codex_meta("user", tid, cwd=str(self.repo))
        records = [meta] + [
            (user_msg if n % 2 == 0 else reply)(n + 1, text)
            for n, text in enumerate(texts)
        ]
        return self.write(rollout(tid), records)

    def pctx(self, *argv, env=None):
        """(exit code, parsed JSON or raw text, stderr) of one call."""
        out, err, cwd = io.StringIO(), io.StringIO(), os.getcwd()
        patch = mock.patch.dict(os.environ, self.env | (env or {}), clear=True)
        with (
            patch,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            os.chdir(self.repo)
            try:
                code = cli.main(list(argv))
            finally:
                os.chdir(cwd)
        text = out.getvalue()
        try:
            return code, json.loads(text), err.getvalue()
        except ValueError:
            return code, text, err.getvalue()


class ReaderTests(CliCase):
    def setUp(self):
        super().setUp()
        self.session(TID, f"where is the {CANARY} config", "in the repo root")
        self.run_ingest()

    def test_search_shape_and_freshness(self):
        code, out, _ = self.pctx("search", CANARY)
        self.assertEqual(code, 0)
        self.assertLessEqual(
            ALWAYS | {"hits", "knowledge", "stages"}, set(out)
        )
        self.assertEqual(out["poller"], "stale")  # no heartbeat yet
        self.assertEqual(out["hits"][0]["ref"], f"codex:{TID}:2.1")
        plain = out["hits"][0]["snippet"].replace("«", "").replace("»", "")
        self.assertIn(CANARY, plain)

    def test_open_sessions_session_quote_check_shapes(self):
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

    def test_exit_codes(self):
        self.assertEqual(self.pctx("open", "codex:nope:1.1")[0], 2)
        self.assertEqual(self.pctx("search")[0], 2)  # usage
        code, out, _ = self.pctx(
            "search", "x", env={"PCTX_HOME": str(self.tmp / "empty")}
        )
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertIn("notice", out)
        with (
            mock.patch.object(
                cli.store, "connect_ro", side_effect=store.HotJournal("hot")
            ),
            mock.patch.object(
                cli.store, "heal_hot_journal", return_value=False
            ),
        ):  # sandboxed
            code, out, _ = self.pctx("search", CANARY)
        self.assertEqual((code, out["error"]), (4, "hot_journal"))

    def test_reader_heals_hot_journal_unsandboxed(self):
        child = Child(self, SPILLING_WRITER, store.db_path(self.home))
        child.wait_ready()
        child.proc.kill()  # a crashed writer leaves a hot journal
        child.proc.wait()
        journal = self.home / "pctx.sqlite-journal"
        self.assertTrue(journal.exists())
        code, out, _ = self.pctx("search", CANARY)
        self.assertEqual(code, 0, out)
        self.assertFalse(journal.exists())

    def test_output_time_redaction(self):
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
        with open(path, "ab") as handle:  # raw line with a secret field
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

    def test_logged_flag_denied_and_disabled(self):
        _, out, _ = self.pctx("search", CANARY)
        self.assertIs(out["logged"], True)
        _, out, _ = self.pctx("search", CANARY, env={"PCTX_NO_CALLLOG": "1"})
        self.assertIs(out["logged"], False)
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        (self.home / "calls.jsonl").chmod(0o400)
        _, out, _ = self.pctx("search", CANARY)
        self.assertIs(out["logged"], False)

    def test_calls_log_has_no_text(self):
        self.pctx("search", CANARY)
        self.pctx("open", f"codex:{TID}:2.1")
        blob = (self.home / "calls.jsonl").read_text()
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("CANARY", blob.upper())
        lines = [json.loads(x) for x in blob.splitlines()]
        self.assertEqual([x["cmd"] for x in lines], ["search", "open"])
        self.assertEqual(lines[0]["n_terms"] >= 1, True)
        self.assertEqual(lines[1]["target_id"] > 0, True)


class WriterTests(CliCase):
    def test_ingest_command_and_heartbeat(self):
        self.session(TID, "hello there", "hi")
        code, out, _ = self.pctx("ingest")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["ingest"]["events_added"], 2)
        status = json.loads((self.home / "status.json").read_text())
        self.assertLessEqual(
            {
                "last_pass_at",
                "files_changed",
                "failed",
                "errors",
                "classifier_version",
            },
            set(status),
        )
        _, found, _ = self.pctx("search", "hello")
        self.assertEqual(found["poller"], "ok")
        code, out, _ = self.pctx("ingest", "--full")
        self.assertEqual(out["ingest"]["events_removed"], 2)

    def test_erase_command_needs_yes(self):
        self.session(TID, "hello there", "hi")
        self.run_ingest()
        code, out, _ = self.pctx("erase", "--session", TID)
        self.assertEqual((code, out["dry_run"], out["events"]), (0, True, 2))
        self.assertIn("--yes", out["note"])
        self.assertEqual(len(self.events()), 2)
        code, out, _ = self.pctx("erase", "--session", TID, "--yes")
        self.assertEqual((code, out["dry_run"], out["residue"]), (0, False, 0))
        self.assertEqual(self.events(), [])
        self.assertEqual(
            self.pctx("erase", "--session", "a", "--match", "bcdef")[0], 2
        )
        code, out, _ = self.pctx("erase", "--match", "ab", "--yes")
        self.assertEqual((code, out["error"]), (2, "refused"))

    def test_busy_exit_3(self):
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.pctx("ingest")
        self.assertEqual((code, out["error"]), (3, "busy"))


LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "pctx"


class ServeTests(CliCase):
    def start_serve(self, interval="0.2"):
        log = open(self.home / "poller.log", "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [str(LAUNCHER), "serve", "--interval", interval],
            env=self.env,
            stdout=log,
            stderr=log,
            cwd=self.repo,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def wait_status(self, key, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = obs.read_status(self.home)
            if status.get(key):
                return status
            time.sleep(0.05)
        self.fail(f"no {key} in status.json")

    def test_serve_sigterm_between_sources(self):
        self.session(TID, f"{CANARY} in a prompt", "a reply")
        proc = self.start_serve()
        status = self.wait_status("passes")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertFalse((self.home / "pctx.sqlite-journal").exists())
        self.assertEqual(status["pid"], proc.pid)
        self.assertEqual(status["interval_s"], 0.2)
        self.assertLessEqual(
            {
                "last_pass_at",
                "files_seen",
                "events_added",
                "failed",
                "errors",
                "classifier_version",
                "schema_version",
                "install_sha",
                "duration_s",
            },
            set(status),
        )
        self.assertEqual(status["events_added"], 2)
        self.pctx("search", CANARY)
        for name in ("calls.jsonl", "status.json", "poller.log"):
            with self.subTest(file=name):
                self.assertNotIn(CANARY, (self.home / name).read_text())

    def test_serve_sigterm_mid_pass_leaves_no_journal(self):
        for n in range(300):
            self.session(f"thr-{n:04d}", f"prompt {n}", f"reply {n}")
        proc = self.start_serve(interval="60")
        time.sleep(0.4)  # most likely inside the first pass
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertFalse((self.home / "pctx.sqlite-journal").exists())
        conn = store.connect_ro(store.db_path(self.home))
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )
        torn = conn.execute(
            "SELECT count(*) FROM source s WHERE s.cursor_line > 0 AND"
            " (SELECT count(*) FROM event e WHERE e.source_id = s.id) != 2"
        ).fetchone()[0]
        self.assertEqual(torn, 0)  # every committed source is whole

    def test_status_json_heartbeat_and_poller_stale(self):
        self.session(TID, "hello there", "hi")
        self.run_ingest()
        obs.write_status(
            self.home, {"last_pass_at": time.time() - 1, "interval_s": 60}
        )
        self.assertEqual(self.pctx("search", "hello")[1]["poller"], "ok")
        obs.write_status(self.home, {"last_pass_at": time.time() - 500})
        _, out, _ = self.pctx("search", "hello")
        self.assertEqual(out["poller"], "stale")
        self.assertGreaterEqual(out["index_age_s"], 500)
