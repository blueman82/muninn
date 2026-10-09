"""muninn stats, doctor, compact and the shape every answer shares."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Any
from unittest import mock

from muninn import cli, obs, store
from tests.cli_support import ALWAYS, CliCase, fake_run
from tests.store_support import public_read
from tests.test_classify import codex_meta, function_call
from tests.test_ingest import TID, fc_output, rollout
from tests.test_store import SPILLING_WRITER, Child

Runner = Callable[[Sequence[object]], CompletedProcess[bytes]]


class StatsDoctorTests(CliCase):
    """Usage statistics and the doctor health checks."""

    def muninn_call(
        self, n: int, cid: str, command: str, output: str
    ) -> list[dict[str, Any]]:
        """Build the rollout records of one shell call and its output.

        Args:
            n: Ordinal of the call record; the output takes ``n + 1``.
            cid: Call id linking the call to its output.
            command: Shell command the call ran.
            output: Text the shell printed.

        Returns:
            The call record followed by its output record.
        """
        return [
            function_call(
                n, "exec_command", json.dumps({"cmd": command}), cid
            ),
            fc_output(n + 1, cid, output),
        ]

    def test_stats_usage(self) -> None:
        meta = codex_meta("user", TID, cwd=str(self.repo))
        exited = "Process exited with code {}\n"
        self.write(
            rollout(),
            [
                meta,
                *self.muninn_call(
                    1, "p1", "muninn search x", exited.format(3)
                ),
                *self.muninn_call(3, "p2", "muninn stats", exited.format(0)),
            ],
        )
        self.run_ingest()
        code, out, _ = self.muninn("stats", "--usage")
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
                "reread",
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

    def doctor(
        self, *extra: str, run: Runner | None = None
    ) -> tuple[int, Any, str]:
        """Run ``muninn doctor`` with launchctl and ps faked out.

        Args:
            *extra: Further command-line flags.
            run: Replacement for ``obs.run``; defaults to a healthy job.

        Returns:
            Exit code, parsed JSON and stderr of the call.
        """

        def uid() -> int:
            return 501

        native_access = SimpleNamespace(
            getuid=uid, access=os.access, R_OK=os.R_OK, X_OK=os.X_OK
        )
        with (
            mock.patch.object(cli.obs, "run", run or fake_run()),
            mock.patch.object(
                cli.obs, "sys", SimpleNamespace(platform="darwin")
            ),
            mock.patch.object(cli.obs, "os", native_access),
        ):
            return self.muninn("doctor", *extra)

    def checks(self, out: dict[str, Any]) -> dict[str, bool]:
        """Map each doctor check name to whether it passed.

        Args:
            out: Parsed ``doctor`` output.

        Returns:
            Check name to its ``ok`` flag.
        """
        return {c["check"]: c["ok"] for c in out["checks"]}

    def test_fake_launchd_keeps_native_modules_and_supplies_uid(self) -> None:
        self.session(TID, "hello", "hi")
        self.assertEqual(self.muninn("ingest")[0], 0)
        native_platform = sys.platform
        fake = fake_run()
        calls: list[Sequence[object]] = []

        def run(argv: Sequence[object]) -> CompletedProcess[bytes]:
            self.assertIsNot(obs.sys, sys)
            self.assertIsNot(obs.os, os)
            self.assertEqual(sys.platform, native_platform)
            self.assertIs(obs.os.access, os.access)
            calls.append(argv)
            return fake(argv)

        without_uid = SimpleNamespace(
            access=os.access, R_OK=os.R_OK, X_OK=os.X_OK
        )
        with mock.patch.object(obs, "os", without_uid):
            code, out, _ = self.doctor(run=run)
        self.assertEqual(code, 0, out)
        self.assertEqual(
            list(calls[0]), ["launchctl", "print", "gui/501/com.muninn"]
        )
        self.assertIs(obs.sys, sys)
        self.assertIs(obs.os, os)
        self.assertEqual(sys.platform, native_platform)

    def test_unsafe_status_control_retains_bytes_and_restores_privacy(
        self,
    ) -> None:
        self.session(TID, "hello", "hi")
        self.assertEqual(self.muninn("ingest")[0], 0)
        path = self.home / "status.json"
        original = path.read_bytes()
        with public_read(self, path):
            code, out, _ = self.doctor()
            self.assertEqual(code, 1, out)
            self.assertIs(self.checks(out)["file_modes"], False)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.doctor()[0], 0)

    def test_doctor_checks(self) -> None:
        path = self.session(TID, "hello there", "hi")
        self.session("thr-two", "second", "one")
        self.assertEqual(self.muninn("ingest")[0], 0)
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
        self.muninn("ingest")
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
        kept = self.home / f"{store.UNREADABLE_PREFIX}20260101T000000Z"
        kept.touch(0o600)  # a store that rebuild set aside is known
        self.assertIs(self.checks(self.doctor()[1])["unexpected_files"], True)
        kept.unlink()
        (self.home / "recall.off").touch(0o600)  # the recall switch is known
        self.assertIs(self.checks(self.doctor()[1])["unexpected_files"], True)
        status = self.home / "status.json"
        original = status.read_bytes()
        with public_read(self, status):
            self.assertIs(self.checks(self.doctor()[1])["file_modes"], False)
            self.assertEqual(status.read_bytes(), original)
        self.assertEqual(self.doctor(run=fake_run(pid=None))[0], 1)
        obs.write_status(self.home, {"last_pass_at": time.time() - 900})
        code, out, _ = self.doctor()
        self.assertEqual((code, self.checks(out)["heartbeat"]), (1, False))

    def test_doctor_warns_on_bloat_and_compact_reclaims(self) -> None:
        self.session(TID, "hello there", "hi")
        self.muninn("ingest")
        conn = cli.store.connect_rw(cli.store.db_path(self.home))
        conn.execute("CREATE TABLE junk(x)")
        conn.execute("INSERT INTO junk VALUES (zeroblob(500000))")
        conn.execute("DELETE FROM junk")  # frees the pages, keeps the file
        conn.close()
        with (
            mock.patch.object(cli.obs, "FREE_WARN_BYTES", 0),
            mock.patch.object(cli.obs, "FREE_WARN_RATIO", 0.0),
        ):
            code, out, _ = self.doctor()
        warn = {c["check"]: c for c in out["checks"]}["db_free_space"]
        self.assertEqual((code, warn["ok"], warn["level"]), (0, False, "warn"))
        self.assertGreater(
            self.muninn("stats")[1]["db_space"]["freelist_count"], 0
        )
        code, out, _ = self.muninn("compact")
        self.assertEqual(code, 0, out)
        self.assertLess(
            out["compact"]["bytes_after"], out["compact"]["bytes_before"]
        )
        self.assertEqual(out["db_space"]["freelist_count"], 0)

    def test_pretty_is_indented_same_json(self) -> None:
        self.session(TID, "hello there", "hi")
        self.muninn("ingest")

        def raw(*argv: str) -> str:
            out = io.StringIO()
            with (
                mock.patch.dict(os.environ, self.env, clear=True),
                contextlib.redirect_stdout(out),
            ):
                cli.main(list(argv))
            return out.getvalue()

        compact, pretty = raw("stats"), raw("--pretty", "stats")
        self.assertNotIn("\n", compact.strip())
        self.assertIn('\n  "', pretty)
        self.assertEqual(set(json.loads(compact)), set(json.loads(pretty)))

    def test_doctor_reports_unowned_journal(self) -> None:
        self.session(TID, "hello", "hi")
        self.muninn("ingest")
        child = Child(self, SPILLING_WRITER, store.db_path(self.home))
        child.wait_ready()
        child.proc.kill()
        child.proc.wait()
        code, out, _ = self.doctor()
        self.assertEqual(
            (code, self.checks(out)["unowned_journal"]), (1, False)
        )
        self.assertTrue((self.home / "muninn.sqlite-journal").exists())


class GoldenTests(CliCase):
    """Every command answers with the same envelope fields."""

    def test_every_answer_has_notice_freshness_and_logged(self) -> None:
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
                    _, out, _ = self.muninn(*argv)
                self.assertIsInstance(out, dict)
                self.assertLessEqual(ALWAYS, set(out))
                self.assertEqual(out["notice"], cli.NOTICE)
        with mock.patch.object(cli.obs, "run", fake_run()):
            _, out, _ = self.muninn("doctor")
        self.assertLessEqual(ALWAYS | {"ok", "checks"}, set(out))
