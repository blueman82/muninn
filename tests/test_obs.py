"""Observability contract (design 7; spec O8d, O9, A14): stage log with an
allowlist and rotation, status heartbeat, no transcript text anywhere."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pctx import obs

CANARY = "CANARY-OBS-" + "z9" * 8


class ObsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.log = self.home / "calls.jsonl"

    def lines(self, path=None):
        path = path or self.log
        return [json.loads(x) for x in path.read_text().splitlines()]


class CallLogTests(ObsCase):
    def test_allowlist_drops_text_and_unknown_fields(self):
        ok = obs.log_call(
            self.home,
            {
                "cmd": "search",
                "actor": "claude:1a23fdfe-0f0",
                "n_terms": 3,
                "query_sha12": "0123456789ab",
                "stages": {"fts": 5, "returned": 2, "text": CANARY},
                "returned_ids": [1, 2, CANARY],
                "exit": 0,
                "ms": 4.5,
                "query": CANARY,  # never logged
                "text": CANARY,
                "error": f"bad {CANARY}",  # not a code: dropped
            },
        )
        self.assertTrue(ok)
        (line,) = self.lines()
        self.assertNotIn(CANARY, self.log.read_text())
        self.assertEqual(line["stages"], {"fts": 5, "returned": 2})
        self.assertEqual(line["returned_ids"], [1, 2])
        self.assertEqual(
            set(line),
            {
                "at",
                "cmd",
                "actor",
                "n_terms",
                "query_sha12",
                "stages",
                "returned_ids",
                "exit",
                "ms",
            },
        )
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)

    def test_line_is_at_most_1kib(self):
        obs.log_call(
            self.home, {"cmd": "search", "returned_ids": list(range(10_000))}
        )
        self.assertLessEqual(len(self.log.read_bytes()), 1024)

    def test_log_rotation_1mib(self):
        self.log.write_bytes(b"x" * (obs.ROTATE_BYTES - 10) + b"\n")
        obs.log_call(self.home, {"cmd": "stats"})
        rotated = self.home / "calls.jsonl.1"
        self.assertEqual(rotated.stat().st_size, obs.ROTATE_BYTES - 9)
        self.assertEqual([x["cmd"] for x in self.lines()], ["stats"])
        self.log.write_bytes(b"y" * obs.ROTATE_BYTES)
        obs.log_call(self.home, {"cmd": "doctor"})  # .1 replaced: x2 cap
        self.assertTrue(rotated.read_bytes().startswith(b"y"))
        self.assertEqual(
            sorted(p.name for p in self.home.iterdir()),
            ["calls.jsonl", "calls.jsonl.1"],
        )

    def test_logged_false_when_append_denied(self):
        self.home.chmod(0o500)  # the Codex sandbox denies the append
        self.addCleanup(self.home.chmod, 0o700)
        self.assertFalse(obs.log_call(self.home, {"cmd": "search"}))

    def test_no_calllog_env(self):
        env = {"PCTX_NO_CALLLOG": "1"}
        self.assertFalse(obs.log_call(self.home, {"cmd": "search"}, env))
        self.assertFalse(self.log.exists())

    def test_actor_from_env(self):
        cases = (
            (
                {"CLAUDE_CODE_SESSION_ID": "1a23fdfe-0f0c-4c1e"},
                "claude:1a23fdfe-0f0",
            ),
            ({"CODEX_THREAD_ID": "0199aaaa-bbbb-4ccc"}, "codex:0199aaaa-bbb"),
            ({}, "user"),
        )
        for env, want in cases:
            with self.subTest(env=env):
                self.assertEqual(obs.actor(env), want)


class HumanBytesTests(unittest.TestCase):
    def test_units(self):
        for n, want in (
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (197660672, "188.5 MB"),
            (2 * 1024**3, "2.0 GB"),
            (3 * 1024**4, "3.0 TB"),
            (5000 * 1024**4, "5000.0 TB"),
        ):
            self.assertEqual(obs.human_bytes(n), want)


class PollerLogTests(ObsCase):
    def test_line_is_allowlisted_and_log_rotates(self):
        path = self.home / "poller.log"
        self.assertTrue(
            obs.log_poller(
                self.home,
                {"event": "error", "exc": "OSError", "text": CANARY},
            )
        )
        (line,) = self.lines(path)
        self.assertEqual(set(line), {"at", "event", "exc"})
        self.assertNotIn(CANARY, path.read_text())
        path.write_bytes(b"x" * obs.ROTATE_BYTES)
        obs.log_poller(self.home, {"event": "start", "pid": 1})
        self.assertEqual(len(self.lines(path)), 1)
        rotated = path.with_name("poller.log.1")
        self.assertEqual(rotated.stat().st_size, obs.ROTATE_BYTES)


class StatusTests(ObsCase):
    def test_status_heartbeat_merges_and_is_private(self):
        obs.write_status(self.home, {"interval_s": 60, "pid": 7})
        obs.write_status(self.home, {"last_pass_at": 123.0})
        got = obs.read_status(self.home)
        self.assertEqual(
            (got["interval_s"], got["pid"], got["last_pass_at"]),
            (60, 7, 123.0),
        )
        path = self.home / "status.json"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_read_status_missing_or_corrupt_is_empty(self):
        self.assertEqual(obs.read_status(self.home), {})
        (self.home / "status.json").write_text("{not json")
        self.assertEqual(obs.read_status(self.home), {})

    def test_install_sha(self):
        home = Path(tempfile.mkdtemp(dir=self.home))
        self.assertIsNone(obs.install_sha({"HOME": str(home)}))
        lib = home / ".local/lib/provenance-context"
        (lib / "abc1234").mkdir(parents=True)
        (lib / "current").symlink_to(lib / "abc1234")
        self.assertEqual(obs.install_sha({"HOME": str(home)}), "abc1234")
        env = {"HOME": str(home), "PCTX_INSTALL_SHA": "fff0000"}
        self.assertEqual(obs.install_sha(env), "fff0000")

    def test_poller_freshness(self):
        now = time.time()
        with mock.patch("time.time", return_value=now):
            fresh = obs.freshness({"last_pass_at": now - 30, "interval_s": 60})
            stale = obs.freshness(
                {"last_pass_at": now - 500, "interval_s": 60}
            )
            none = obs.freshness({})
        self.assertEqual(fresh, {"index_age_s": 30, "poller": "ok"})
        self.assertEqual(stale["poller"], "stale")
        self.assertEqual(none, {"index_age_s": None, "poller": "stale"})
