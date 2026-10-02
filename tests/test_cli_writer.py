"""muninn writer commands, the bin/muninn launcher and the serve poller."""

from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli_core, obs, store
from muninn.query.index_age import ALIVE_AT
from tests.cli_support import CANARY, CliCase
from tests.test_ingest import TID


class WriterTests(CliCase):
    """Commands that take the writer lock."""

    def test_ingest_command_and_heartbeat(self) -> None:
        self.session(TID, "hello there", "hi")
        code, out, _ = self.muninn("ingest")
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
        _, found, _ = self.muninn("search", "hello")
        self.assertEqual(found["poller"], "ok")
        code, out, _ = self.muninn("ingest", "--full")
        self.assertEqual(out["ingest"]["events_removed"], 2)

    def test_erase_command_needs_yes(self) -> None:
        self.session(TID, "hello there", "hi")
        self.run_ingest()
        code, out, _ = self.muninn("erase", "--session", TID)
        self.assertEqual((code, out["dry_run"], out["events"]), (0, True, 2))
        self.assertIn("--yes", out["note"])
        self.assertEqual(len(self.events()), 2)
        code, out, _ = self.muninn("erase", "--session", TID, "--yes")
        self.assertEqual((code, out["dry_run"], out["residue"]), (0, False, 0))
        self.assertEqual(self.events(), [])
        self.assertEqual(
            self.muninn("erase", "--session", "a", "--match", "bcdef")[0], 2
        )
        code, out, _ = self.muninn("erase", "--match", "ab", "--yes")
        self.assertEqual((code, out["error"]), (2, "refused"))

    def test_busy_exit_3(self) -> None:
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(cli_core, "WRITER_WAIT_S", 0),
        ):
            code, out, _ = self.muninn("ingest")
        self.assertEqual((code, out["error"]), (3, "busy"))


LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "muninn"


class LauncherTests(unittest.TestCase):
    """bin/muninn finds its interpreter without a hardcoded path."""

    def launch(
        self, home: Path, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Run ``bin/muninn --version`` with a minimal environment.

        Args:
            home: Directory used as ``HOME``.
            **env: Variables that override the minimal environment.

        Returns:
            The completed process with decoded output.
        """
        base = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            [str(LAUNCHER), "--version"],
            env=base | env,
            capture_output=True,
            text=True,
        )

    def fake_python(self, path: Path) -> Path:
        """Write an executable that stands in for a Python interpreter.

        Args:
            path: Where to create the stand-in.

        Returns:
            The same path.
        """
        path.write_text('#!/bin/sh\necho "ran $0"\n')
        path.chmod(0o755)
        return path

    def test_resolution_order_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            lib = home / ".local/lib/muninn"
            lib.mkdir(parents=True)
            linked = self.fake_python(home / "linked-python")
            (lib / "python").symlink_to(linked)
            self.assertIn("muninn/python", self.launch(home).stdout)
            env_py = self.fake_python(home / "env-python")
            r = self.launch(home, MUNINN_PYTHON=str(env_py))
            self.assertIn("env-python", r.stdout)
            (lib / "python").unlink()
            stubs = home / "stubs"  # pythons older than 3.13
            stubs.mkdir()
            for name in ("python3.13", "python3.14", "python3"):
                (stubs / name).write_text("#!/bin/sh\nexit 1\n")
                (stubs / name).chmod(0o755)
            r = self.launch(home, PATH=f"{stubs}:/usr/bin:/bin")
            self.assertEqual(r.returncode, 127)
            self.assertIn("MUNINN_PYTHON", r.stderr)


class ServeTests(CliCase):
    """Run the poller as a real subprocess through the launcher."""

    def start_serve(self, interval: str = "0.2") -> subprocess.Popen[bytes]:
        """Start ``muninn serve`` with its output in the poller log.

        Args:
            interval: Seconds between passes.

        Returns:
            The running process; cleanup waits for or kills it.
        """
        log_path = self.home / "poller.log"
        log = self.enterContext(log_path.open("ab"))
        log_path.chmod(0o644)  # as launchd creates it
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

    def wait_status(self, key: str, timeout: float = 60) -> dict[str, Any]:
        """Wait until the heartbeat file reports a truthy ``key``.

        Args:
            key: Field that must appear in ``status.json``.
            timeout: Seconds to wait before failing the test.

        Returns:
            The status document that contains the field.

        Raises:
            AssertionError: If the field never appears.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = obs.read_status(self.home)
            if status.get(key):
                return status
            time.sleep(0.05)
        raise AssertionError(f"no {key} in status.json")

    def wait_started(self, timeout: float = 60) -> None:
        """Wait until the poller has installed its handlers and said so.

        The ``start`` line is logged after the SIGTERM handler is installed,
        so a signal sent once it appears is always handled. Signalling a
        process that is still starting up kills it with the default action
        (exit -15), which is what made this test flaky under load.

        Args:
            timeout: Seconds to wait before failing the test.

        Raises:
            AssertionError: If the poller never logs its start.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = (self.home / "poller.log").read_text()
            if '"event":"start"' in text:
                return
            time.sleep(0.05)
        raise AssertionError("poller never logged its start")

    def test_serve_sigterm_between_sources(self) -> None:
        self.session(TID, f"{CANARY} in a prompt", "a reply")
        proc = self.start_serve()
        status = self.wait_status("passes")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertFalse((self.home / "muninn.sqlite-journal").exists())
        self.assertEqual(status["pid"], proc.pid)
        self.assertEqual(status["interval_s"], 0.2)
        self.assertLessEqual(
            {
                "last_pass_at",
                ALIVE_AT,
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
        log_mode = (self.home / "poller.log").stat().st_mode & 0o777
        self.assertEqual(log_mode, 0o600)
        events = [
            json.loads(x)
            for x in (self.home / "poller.log").read_text().splitlines()
        ]
        self.assertEqual(
            [e["event"] for e in events][0 :: len(events) - 1],
            ["start", "stop"],
        )
        (first,) = [e for e in events if e["event"] == "pass"][:1]
        self.assertEqual(first["events_added"], 2)
        self.muninn("search", CANARY)
        for name in ("calls.jsonl", "status.json", "poller.log"):
            with self.subTest(file=name):
                self.assertNotIn(CANARY, (self.home / name).read_text())

    def test_serve_sigterm_mid_pass_leaves_no_journal(self) -> None:
        for n in range(300):
            self.session(f"thr-{n:04d}", f"prompt {n}", f"reply {n}")
        proc = self.start_serve(interval="60")
        self.wait_started()
        time.sleep(0.2)  # most likely inside the first pass
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertFalse((self.home / "muninn.sqlite-journal").exists())
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

    def test_status_json_heartbeat_and_poller_stale(self) -> None:
        self.session(TID, "hello there", "hi")
        self.run_ingest()
        obs.write_status(
            self.home, {"last_pass_at": time.time() - 1, "interval_s": 60}
        )
        self.assertEqual(self.muninn("search", "hello")[1]["poller"], "ok")
        obs.write_status(self.home, {"last_pass_at": time.time() - 500})
        _, out, _ = self.muninn("search", "hello")
        self.assertEqual(out["poller"], "stale")
        self.assertGreaterEqual(out["index_age_s"], 500)
