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
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pctx import cli, cli_core, ingest, knowledge, obs, store
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
                cli.store, "connect_ro", side_effect=store.HotJournalError("hot")
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
            mock.patch.object(cli_core, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.pctx("ingest")
        self.assertEqual((code, out["error"]), (3, "busy"))


LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "pctx"


class LauncherTests(unittest.TestCase):
    """bin/pctx finds its interpreter without a hardcoded path."""

    def launch(self, home, **env):
        base = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            [str(LAUNCHER), "--version"],
            env=base | env,
            capture_output=True,
            text=True,
        )

    def fake_python(self, path):
        path.write_text('#!/bin/sh\necho "ran $0"\n')
        path.chmod(0o755)
        return path

    def test_resolution_order_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lib = home / ".local/lib/provenance-context"
            lib.mkdir(parents=True)
            linked = self.fake_python(home / "linked-python")
            (lib / "python").symlink_to(linked)
            self.assertIn("provenance-context/python", self.launch(home).stdout)
            env_py = self.fake_python(home / "env-python")
            r = self.launch(home, PCTX_PYTHON=str(env_py))
            self.assertIn("env-python", r.stdout)
            (lib / "python").unlink()
            stubs = home / "stubs"  # pythons older than 3.13
            stubs.mkdir()
            for name in ("python3.13", "python3.14", "python3"):
                (stubs / name).write_text("#!/bin/sh\nexit 1\n")
                (stubs / name).chmod(0o755)
            r = self.launch(home, PATH=f"{stubs}:/usr/bin:/bin")
            self.assertEqual(r.returncode, 127)
            self.assertIn("PCTX_PYTHON", r.stderr)


class ServeTests(CliCase):
    def start_serve(self, interval="0.2"):
        log = open(self.home / "poller.log", "ab")
        os.chmod(self.home / "poller.log", 0o644)  # as launchd creates it
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
        # launchd opens StandardOutPath before Umask applies: 0644 -> 0600
        log_mode = os.stat(self.home / "poller.log").st_mode & 0o777
        self.assertEqual(log_mode, 0o600)
        events = [
            json.loads(x)
            for x in (self.home / "poller.log").read_text().splitlines()
        ]
        self.assertEqual(
            [e["event"] for e in events][0::len(events) - 1], ["start", "stop"]
        )
        (first,) = [e for e in events if e["event"] == "pass"][:1]
        self.assertEqual(first["events_added"], 2)
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


def fake_run(pid=4242, cmd=None, ps=""):
    """launchctl/ps stand-in: never the real launchd domain."""

    def run(argv):
        argv = [str(a) for a in argv]
        if argv[:2] == ["launchctl", "print"]:
            if pid is None:
                return subprocess.CompletedProcess(argv, 113, b"", b"")
            return subprocess.CompletedProcess(
                argv, 0, f"\tstate = running\n\tpid = {pid}\n".encode(), b""
            )
        if argv[:2] == ["ps", "-ww"] and "-p" in argv:
            return subprocess.CompletedProcess(
                argv, 0, (cmd or "").encode(), b""
            )
        return subprocess.CompletedProcess(argv, 0, ps.encode(), b"")

    return run


class StatsDoctorTests(CliCase):
    def pctx_call(self, n, cid, command, output):
        from tests.test_classify import function_call
        from tests.test_ingest import fc_output

        return [
            function_call(
                n, "exec_command", json.dumps({"cmd": command}), cid
            ),
            fc_output(n + 1, cid, output),
        ]

    def test_stats_usage(self):
        meta = codex_meta("user", TID, cwd=str(self.repo))
        exited = "Process exited with code {}\n"
        self.write(
            rollout(),
            [meta]
            + self.pctx_call(1, "p1", "pctx search x", exited.format(3))
            + self.pctx_call(3, "p2", "pctx stats", exited.format(0)),
        )
        self.run_ingest()
        code, out, _ = self.pctx("stats", "--usage")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            out["usage"],
            [
                {
                    "provider": "codex",
                    "session_root": TID,
                    "calls": 2,
                    "errors": 1,
                    "last_ts": meta["timestamp"],
                }
            ],
        )
        self.assertEqual(
            out["usage_totals"],
            {"codex": {"calls": 2, "errors": 1, "sessions": 1}},
        )
        self.assertLessEqual(
            {
                "sources",
                "events",
                "events_by_provider",
                "flags",
                "issues",
                "knowledge",
                "citations",
                "tombstones",
                "db_bytes",
                "db_space",
                "last_pass",
                "classifier_version",
                "hash_mismatches",
            }
            | ALWAYS,
            set(out),
        )
        self.assertEqual(out["events"], {"tool_call": 2})
        self.assertEqual(sum(out["events_by_provider"].values()), 2)
        self.assertIn("skipped_files", out["last_pass"])
        self.assertEqual(out["flags"]["marker"], 2)

    def doctor(self, *extra, run=None):
        with mock.patch.object(cli.obs, "run", run or fake_run()):
            return self.pctx("doctor", *extra)

    def checks(self, out):
        return {c["check"]: c["ok"] for c in out["checks"]}

    def test_doctor_checks(self):
        path = self.session(TID, "hello there", "hi")
        self.session("thr-two", "second", "one")
        self.assertEqual(self.pctx("ingest")[0], 0)
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        got = self.checks(out)
        for name in (
            "data_dir_mode",
            "file_modes",
            "unexpected_files",
            "journal_mode",
            "writer_secure_delete",
            "fts_secure_delete",
            "quick_check",
            "heartbeat",
            "launchd_job",
            "roots_readable",
            "unowned_journal",
        ):
            with self.subTest(check=name):
                self.assertIs(got[name], True)
        path.unlink()
        self.pctx("ingest")
        code, out, _ = self.doctor()  # missing sources are not errors
        info = {c["check"]: c for c in out["checks"]}["missing_sources"]
        self.assertEqual(
            (code, info["level"], info["detail"]), (0, "info", "1")
        )
        stray = self.home / "stray.bak"
        stray.write_text("x")
        code, out, _ = self.doctor()
        self.assertEqual(
            (code, self.checks(out)["unexpected_files"]), (1, False)
        )
        stray.unlink()
        (self.home / "recall.off").touch(0o600)  # the recall switch is known
        self.assertIs(self.checks(self.doctor()[1])["unexpected_files"], True)
        (self.home / "status.json").chmod(0o644)
        self.assertIs(self.checks(self.doctor()[1])["file_modes"], False)
        (self.home / "status.json").chmod(0o600)
        self.assertEqual(self.doctor(run=fake_run(pid=None))[0], 1)
        obs.write_status(self.home, {"last_pass_at": time.time() - 900})
        code, out, _ = self.doctor()
        self.assertEqual((code, self.checks(out)["heartbeat"]), (1, False))

    def test_doctor_warns_on_bloat_and_compact_reclaims(self):
        self.session(TID, "hello there", "hi")
        self.pctx("ingest")
        conn = cli.store.connect_rw(cli.store.db_path(self.home))
        conn.execute("CREATE TABLE junk(x)")
        conn.execute("INSERT INTO junk VALUES (zeroblob(500000))")
        conn.execute("DELETE FROM junk")  # frees the pages, keeps the file
        conn.close()
        with mock.patch.object(cli.obs, "FREE_WARN_BYTES", 0):
            with mock.patch.object(cli.obs, "FREE_WARN_RATIO", 0.0):
                code, out, _ = self.doctor()
        warn = {c["check"]: c for c in out["checks"]}["db_free_space"]
        self.assertEqual((code, warn["ok"], warn["level"]), (0, False, "warn"))
        self.assertGreater(self.pctx("stats")[1]["db_space"]["freelist_count"], 0)
        code, out, _ = self.pctx("compact")
        self.assertEqual(code, 0, out)
        self.assertLess(
            out["compact"]["bytes_after"], out["compact"]["bytes_before"]
        )
        self.assertEqual(out["db_space"]["freelist_count"], 0)

    def test_pretty_is_indented_same_json(self):
        self.session(TID, "hello there", "hi")
        self.pctx("ingest")

        def raw(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True):
                with contextlib.redirect_stdout(out):
                    cli.main(list(argv))
            return out.getvalue()

        compact, pretty = raw("stats"), raw("--pretty", "stats")
        self.assertNotIn("\n", compact.strip())
        self.assertIn('\n  "', pretty)
        keys = lambda t: set(json.loads(t))  # noqa: E731
        self.assertEqual(keys(compact), keys(pretty))

    def test_doctor_reports_unowned_journal(self):
        self.session(TID, "hello", "hi")
        self.pctx("ingest")
        child = Child(self, SPILLING_WRITER, store.db_path(self.home))
        child.wait_ready()
        child.proc.kill()
        child.proc.wait()
        code, out, _ = self.doctor()
        self.assertEqual(
            (code, self.checks(out)["unowned_journal"]), (1, False)
        )
        self.assertTrue((self.home / "pctx.sqlite-journal").exists())


class RebuildTests(CliCase):
    def setUp(self):
        super().setUp()
        self.keep = self.session(TID, "keep this prompt", "kept")
        self.gone = self.session("thr-gone", "file deleted later", "gone")
        self.session("thr-erased", f"{CANARY} erased", "erased")
        self.run_ingest()
        sid = self.conn.execute("SELECT id FROM scope LIMIT 1").fetchone()[0]
        self.kid = self.conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'decision', 'use rebuild', 'current',"
            " 'user', 1)",
            (sid,),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind, quote, span_start, span_end)"
            " VALUES (?, 'codex', ?, 2, 1, 'h', 'user', 'prompt',"
            " 'keep this', 0, 9)",
            (self.kid, TID),
        )
        self.conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'add', 'user', 1)",
            (self.kid,),
        )
        self.assertEqual(
            self.pctx("erase", "--session", "thr-erased", "--yes")[0], 0
        )
        self.gone.unlink()
        self.run_ingest()  # thr-gone is now missing, its events kept
        self.conn.close()

    def rows(self, sql):
        conn = store.connect_ro(store.db_path(self.home))
        try:
            return [tuple(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def test_rebuild_preserves_knowledge_tombstones_missing_events(self):
        before = os.stat(store.db_path(self.home)).st_ino
        code, out, _ = self.pctx("rebuild")
        self.assertEqual(code, 0, out)
        self.assertIs(out["old_readable"], True)
        self.assertNotEqual(os.stat(store.db_path(self.home)).st_ino, before)
        self.assertEqual(
            self.rows("SELECT id, text, status FROM knowledge"),
            [(self.kid, "use rebuild", "current")],
        )
        self.assertEqual(self.rows("SELECT count(*) FROM citation"), [(1,)])
        self.assertEqual(
            self.rows("SELECT count(*) FROM knowledge_log"), [(1,)]
        )
        threads = dict(self.rows("SELECT thread_id, status FROM source"))
        self.assertEqual(threads, {TID: "active", "thr-gone": "missing"})
        texts = {r[0] for r in self.rows("SELECT text FROM event")}
        self.assertLessEqual({"keep this prompt", "file deleted later"}, texts)
        self.assertFalse(any(CANARY in t for t in texts))  # tombstone held
        self.assertEqual(
            self.rows("SELECT level FROM tombstone"), [("session",)]
        )
        leftovers = [
            p.name
            for p in self.home.iterdir()
            if "rebuild" in p.name or p.name.endswith("-journal")
        ]
        self.assertEqual(leftovers, [])
        self.assertEqual(
            os.stat(store.db_path(self.home)).st_mode & 0o777, 0o600
        )
        found = self.rows(
            "SELECT count(*) FROM knowledge_fts"
            " WHERE knowledge_fts MATCH 'rebuild'"
        )
        self.assertEqual(found, [(1,)])

    def test_rebuild_from_an_unreadable_store_keeps_tombstones(self):
        db = store.db_path(self.home)
        db.write_bytes(b"not a database" * 100)
        code, out, _ = self.pctx("rebuild")
        self.assertEqual((code, out["old_readable"]), (0, False))
        self.assertGreaterEqual(out["reapplied_tombstones"], 1)
        texts = {r[0] for r in self.rows("SELECT text FROM event")}
        self.assertIn("keep this prompt", texts)
        self.assertFalse(any(CANARY in t for t in texts))


class GoldenTests(CliCase):
    def test_every_answer_has_notice_freshness_and_logged(self):
        self.session(TID, "hello there", "hi")
        self.run_ingest()
        ref = f"codex:{TID}:2.1"
        commands = (
            ("search", "hello"),
            ("open", ref),
            ("sessions",),
            ("session", TID),
            ("quote-check", ref, "hello"),
            ("ingest",),
            ("stats",),
            ("stats", "--usage"),
            ("erase", "--session", TID),
            ("open", "codex:nope:1.1"),
            ("rebuild",),
        )
        for argv in commands:
            with self.subTest(argv=argv):
                with mock.patch.object(cli.obs, "run", fake_run()):
                    code, out, _ = self.pctx(*argv)
                self.assertIsInstance(out, dict)
                self.assertLessEqual(ALWAYS, set(out))
                self.assertEqual(out["notice"], cli.NOTICE)
        with mock.patch.object(cli.obs, "run", fake_run()):
            _, out, _ = self.pctx("doctor")
        self.assertLessEqual(ALWAYS | {"ok", "checks"}, set(out))


class KnowTests(CliCase):
    """pctx know add|retract|list|show|check through main()."""

    QUOTE = "the zebra cache stays in place"

    def setUp(self):
        super().setUp()
        self.session(TID, f"we decided {self.QUOTE} for good", "noted")
        self.run_ingest()
        self.ref = f"codex:{TID}:2.1"

    def add(
        self, *extra, text="Keep the zebra cache", kind="decision", env=None
    ):
        return self.pctx(
            "know", "add", "--kind", kind, "--text", text, *extra, env=env
        )

    def test_know_add_end_to_end_with_a_verbatim_quote(self):
        code, out, _ = self.add("--cite", self.ref, "--quote", self.QUOTE)
        self.assertEqual(code, 0, out)
        self.assertLessEqual(ALWAYS | {"entry"}, set(out))
        entry = out["entry"]
        self.assertEqual(
            (entry["id"], entry["status"], entry["kind"], entry["actor"]),
            ("K1", "current", "decision", "user"),
        )
        self.assertEqual(
            [(c["ref"], c["quote"], c["verify"]) for c in entry["cites"]],
            [(self.ref, self.QUOTE, "ok")],
        )
        code, listed, _ = self.pctx("know", "list")
        self.assertEqual((code, listed["count"]), (0, 1))
        self.assertLessEqual(ALWAYS | {"entries", "count"}, set(listed))
        code, shown, _ = self.pctx("know", "show", "K1")
        self.assertEqual(code, 0)
        self.assertLessEqual(ALWAYS | {"entry", "chain", "log"}, set(shown))
        self.assertEqual([x["action"] for x in shown["log"]], ["add"])
        code, checked, _ = self.pctx("know", "check")
        self.assertEqual(
            (code, checked["ok"], checked["problems"]), (0, 1, [])
        )
        code, gone, _ = self.pctx(
            "know", "retract", "K1", "--reason", "changed"
        )
        self.assertEqual((code, gone["entry"]["status"]), (0, "retracted"))
        _, after, _ = self.pctx("know", "list")
        self.assertEqual(after["count"], 0)
        _, every, _ = self.pctx("know", "list", "--status", "all")
        self.assertEqual(every["count"], 1)
        self.assertEqual(self.pctx("know", "list", "--status", "old")[0], 2)

    def test_know_add_refusals_exit_2(self):
        cases = (
            ((), "uncited"),
            (
                ("--cite", self.ref, "--quote", "a quote nobody typed"),
                "quote_not_found",
            ),
            (
                ("--cite", f"codex:{TID}:99.1", "--quote", self.QUOTE),
                "not_found",
            ),
            (("--cite", "garbage", "--quote", self.QUOTE), "bad_ref"),
            (("--cite", self.ref, "--quote", "we decided"), "quote_length"),
        )
        for extra, error in cases:
            with self.subTest(error):
                code, out, _ = self.add(*extra)
                self.assertEqual((code, out["error"]), (2, error))
                self.assertLessEqual(ALWAYS, set(out))
        code, out, _ = self.add(
            "--cite",
            self.ref,
            "--quote",
            self.QUOTE,
            kind="preference",
            text="x" * 501,
        )
        self.assertEqual((code, out["error"]), (2, "text_length"))
        self.assertEqual(
            self.pctx("know", "list")[1]["count"], 0
        )  # nothing written
        self.assertEqual(self.add(kind="rumor")[0], 2)  # argparse: bad choice

    def test_cite_and_quote_pairing(self):
        two = f"codex:{TID}:3.1"
        bad = ("--cite", self.ref, "--quote", "we decided the zebra cache")
        bad += ("--cite", two, "--quote", "noted is one word")
        code, out, _ = self.add(*bad)
        self.assertEqual((code, out["error"]), (2, "quote_not_found"))
        for extra in (
            ("--cite", self.ref),  # a cite without its quote
            ("--cite", self.ref, "--cite", two, "--quote", "we decided"),
            ("--quote", "one quote alone", "--quote", "and another alone"),
        ):
            with self.subTest(extra):
                code, out, _ = self.add(*extra)
                self.assertEqual((code, out["error"]), (2, "refused"))
        self.assertEqual(self.pctx("know", "list")[1]["count"], 0)
        good = ("--cite", self.ref, "--quote", "we decided the zebra")
        good += ("--cite", self.ref, "--quote", "cache stays in place for")
        code, out, _ = self.add(*good)
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [c["quote"] for c in out["entry"]["cites"]],
            ["we decided the zebra", "cache stays in place for"],
        )

    def test_quote_alone_cites_the_callers_fresh_prompt(self):
        path = self.roots["codex-sessions"] / rollout(TID)
        self.append(path, [user_msg(5, f"final: {self.QUOTE}, ship it")])
        env = {"CODEX_THREAD_ID": TID}
        code, out, _ = self.add("--quote", f"final: {self.QUOTE}", env=env)
        self.assertEqual(code, 0, out)
        cite = out["entry"]["cites"][0]
        self.assertEqual(cite["ref"], f"codex:{TID}:4.1")
        self.assertEqual(out["entry"]["actor"], f"codex:{TID[:12]}")
        code, out, _ = self.add(
            "--quote", f"final: {self.QUOTE}"
        )  # no session
        self.assertEqual((code, out["error"]), (2, "no_caller_session"))

    def test_supersede_and_global(self):
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        code, out, _ = self.add(
            "--cite",
            self.ref,
            "--quote",
            self.QUOTE,
            "--supersedes",
            "K1",
            text="Keep it, revised",
        )
        self.assertEqual((code, out["entry"]["supersedes"]), (0, "K1"))
        _, shown, _ = self.pctx("know", "show", "1")
        self.assertEqual(shown["entry"]["status"], "superseded")
        self.assertEqual(
            [c["id"] for c in shown["chain"]["superseded_by"]], ["K2"]
        )
        code, out, _ = self.add(
            "--cite", self.ref, "--quote", self.QUOTE, "--supersedes", "K1"
        )
        self.assertEqual((code, out["error"]), (2, "bad_supersedes"))
        code, out, _ = self.add(
            "--cite", self.ref, "--quote", self.QUOTE, "--global"
        )
        self.assertEqual((code, out["entry"]["scope"]), (0, "global"))
        _, here, _ = self.pctx("know", "list")
        _, wide, _ = self.pctx("know", "list", "--all-projects")
        self.assertEqual((here["count"], wide["count"]), (2, 2))

    def test_know_list_all_projects_reaches_other_scopes(self):
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        knowledge.run_add(
            self.home,
            kind="fact",
            text="Kept in another project",
            cites=[(self.ref, self.QUOTE)],
            cwd=str(elsewhere),
            actor="user",
            roots=ingest.default_roots(self.env),
            env=self.env,
        )
        _, here, _ = self.pctx("know", "list")
        _, wide, _ = self.pctx("know", "list", "--all-projects")
        self.assertEqual([e["id"] for e in here["entries"]], ["K1"])
        self.assertEqual(
            sorted(e["id"] for e in wide["entries"]), ["K1", "K2"]
        )

    def test_show_unknown_and_reader_failures(self):
        self.assertEqual(
            self.pctx("know", "show", "K9")[1]["error"], "not_found"
        )
        self.assertEqual(self.pctx("know", "show", "K9")[0], 2)
        empty = {"PCTX_HOME": str(self.tmp / "empty")}
        for argv in (("list",), ("show", "K1"), ("check",)):
            with self.subTest(argv):
                code, out, _ = self.pctx("know", *argv, env=empty)
                self.assertEqual(
                    (code, out["error"]), (4, "store_unavailable")
                )

    def test_know_writers_are_busy_while_the_lock_is_held(self):
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli_core, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.add("--cite", self.ref, "--quote", self.QUOTE)
            self.assertEqual((code, out["error"]), (3, "busy"))
            code, out, _ = self.pctx("know", "retract", "K1", "--reason", "x")
            self.assertEqual((code, out["error"]), (3, "busy"))

    def test_know_calls_log_ids_not_text(self):
        self.add("--cite", self.ref, "--quote", self.QUOTE)
        self.pctx("know", "list")
        self.add()  # refused
        lines = [
            json.loads(x)
            for x in (self.home / "calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [x["cmd"] for x in lines], ["know add", "know list", "know add"]
        )
        self.assertEqual(lines[0]["knowledge_ids"], [1])
        self.assertEqual(lines[2]["error"], "uncited")
        blob = (self.home / "calls.jsonl").read_text()
        for needle in ("zebra", "decided", "Keep the"):
            self.assertNotIn(needle, blob)
