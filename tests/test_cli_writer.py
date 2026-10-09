"""muninn writer commands, the bin/muninn launcher and the serve poller."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from muninn import cli_core, obs, platform_io, poller_stop, store
from muninn.query.index_age import ALIVE_AT
from tests.cli_support import CANARY, CliCase
from tests.hook_support import launcher_args
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


if sys.platform != "win32":

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
    """Run CLI behavior through POSIX shell or isolated Windows Python."""

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
            launcher_args("serve", "--interval", interval),
            env=self.env,
            stdout=log,
            stderr=log,
            cwd=self.repo,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.proc = proc
        return proc

    def stop(self, status: dict[str, Any]) -> None:
        """Stop the real poller by signal or its published native generation.

        Args:
            status: Heartbeat from this poller process.
        """
        if sys.platform == "win32":
            self.assertEqual(status["pid"], self.proc.pid)
            generation = status["stop_generation"]
            self.assertIsInstance(generation, str)
            poller_stop.request(self.home, self.proc.pid, generation)
            self.assertTrue(
                poller_stop.requested(self.home, self.proc.pid, generation)
            )
        else:
            self.proc.send_signal(signal.SIGTERM)

    def test_start_serve_uses_platform_command_vector(self) -> None:
        command = ["synthetic-python", "isolated", "serve"]
        with (
            mock.patch(
                "tests.test_cli_writer.launcher_args", return_value=command
            ) as arguments,
            mock.patch("tests.test_cli_writer.subprocess.Popen") as start,
        ):
            self.start_serve(interval="17")
        arguments.assert_called_once_with("serve", "--interval", "17")
        self.assertEqual(start.call_args.args[0], command)

    def test_native_stop_targets_the_published_generation(self) -> None:
        self.proc = mock.Mock(pid=4242)
        generation = "a" * 32
        with mock.patch(
            "tests.test_cli_writer.sys",
            SimpleNamespace(platform="win32"),
        ):
            self.stop({"pid": 4242, "stop_generation": generation})
        self.assertTrue(poller_stop.requested(self.home, 4242, generation))
        self.assertFalse(poller_stop.requested(self.home, 4242, "b" * 32))
        self.proc.send_signal.assert_not_called()

    def fail_if_exited(self, what: str) -> None:
        """Fail at once, with a diagnosis, if the poller process is gone.

        Valid only after ``start_serve``. Without this a poller that
        crashes at startup leaves the wait_* helpers spinning for their
        whole timeout with nothing to read.

        Args:
            what: What the caller was still waiting for.

        Raises:
            AssertionError: If the process has exited.
        """
        code = self.proc.poll()
        if code is not None:
            tail = self.log_text()[-500:]
            raise AssertionError(
                f"serve exited with {code} before {what}; poller.log "
                f"ends: {tail!r}"
            )

    def log_text(self) -> str:
        """Return ``poller.log``, or an empty string if it is not there."""
        try:
            return (self.home / "poller.log").read_text()
        except FileNotFoundError:
            return ""

    def wait_for(
        self, what: str, check: Callable[[], Any], timeout: float
    ) -> Any:
        """Poll ``check`` until it returns something truthy.

        Args:
            what: Description of the awaited condition, for failures.
            check: Returns a falsy value until the condition holds.
            timeout: Seconds to wait before failing the test.

        Returns:
            The first truthy value ``check`` returned.

        Raises:
            AssertionError: On timeout, or if serve exited first.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if found := check():
                return found
            if self.proc.poll() is not None:
                # The condition may have become true just before the exit.
                if found := check():
                    return found
                self.fail_if_exited(what)
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def wait_status(self, key: str, timeout: float = 60) -> dict[str, Any]:
        """Wait until the heartbeat file reports a truthy ``key``.

        Args:
            key: Field that must appear in ``status.json``.
            timeout: Seconds to wait before failing the test.

        Returns:
            The status document that contains the field.
        """
        return self.wait_for(
            f"{key} in status.json",
            lambda: (s := obs.read_status(self.home)).get(key) and s,
            timeout,
        )

    def wait_log(self, needle: str, timeout: float = 60) -> None:
        """Wait until ``poller.log`` contains ``needle``.

        Args:
            needle: Text that must appear in the log.
            timeout: Seconds to wait before failing the test.
        """
        self.wait_for(
            f"poller.log to have {needle}",
            lambda: needle in self.log_text(),
            timeout,
        )

    def test_serve_sigterm_during_idle_sleep(self) -> None:
        self.session(TID, f"{CANARY} in a prompt", "a reply")
        # A long interval: after the pass line the poller can only be in
        # its idle sleep, so the signal lands there and not in a second pass.
        proc = self.start_serve(interval="60")
        status = self.wait_status("passes")
        # The heartbeat is written before the pass line, so SIGTERM sent on
        # the heartbeat alone can land between the two and lose the line.
        self.wait_log('"event":"pass"')
        self.stop(status)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertFalse((self.home / "muninn.sqlite-journal").exists())
        self.assertEqual(status["pid"], proc.pid)
        self.assertEqual(status["interval_s"], 60.0)
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
        log_path = self.home / "poller.log"
        if sys.platform == "win32":
            self.assertTrue(platform_io.is_private(log_path))
        else:
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        events = [
            json.loads(x)
            for x in (self.home / "poller.log").read_text().splitlines()
        ]
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "stop")
        (first,) = [e for e in events if e["event"] == "pass"][:1]
        self.assertEqual(first["events_added"], 2)
        self.muninn("search", CANARY)
        for name in ("calls.jsonl", "status.json", "poller.log"):
            with self.subTest(file=name):
                self.assertNotIn(CANARY, (self.home / name).read_text())

    def committed_sources(self) -> int:
        """Count sources the poller has committed, 0 while it is busy."""
        try:
            conn = store.connect_ro(store.db_path(self.home))
        except store.StoreUnavailableError:
            return 0  # no store yet, or a journal mid-write
        try:
            return int(
                conn.execute(
                    "SELECT count(*) FROM source WHERE cursor_line > 0"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            return 0  # locked by the writer: ask again
        finally:
            conn.close()

    def test_serve_sigterm_mid_pass_leaves_no_journal(self) -> None:
        total = 5000  # ~1 ms each to ingest: seconds of window to land in
        for n in range(total):
            self.session(f"thr-{n:04d}", f"prompt {n}", f"reply {n}")
        proc = self.start_serve(interval="60")
        # ALIVE_AT is stamped after planning the first file, before any
        # transaction is open, so it is not proof of a write. Wait for a
        # committed source: the pass is then between transactions of a pass
        # that still has sources left, which is the stop-after-COMMIT path.
        self.wait_for("a committed source", self.committed_sources, 60)
        self.stop(self.wait_status(ALIVE_AT))
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
        done = self.committed_sources()
        # Fails loudly if the stop ever stops landing inside a real pass.
        self.assertGreaterEqual(done, 1)
        self.assertLess(done, total)

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
