"""Who clears the poller's error, and what a crash leaves behind."""

from __future__ import annotations

import os
import time
from argparse import Namespace
from pathlib import Path
from unittest import mock

from muninn import cli_serve, hook_notes, ingest, obs, store
from muninn.cli_core import Result
from muninn.cli_maint import heartbeat
from muninn.ingest import PassStats
from tests.cli_support import CliCase

STOPPED = "the last pass stopped with"


class ErrorClearingTests(CliCase):
    """Only the poller's own good pass clears the poller's error."""

    def setUp(self) -> None:
        super().setUp()
        self.session("thr-clear", "hello")
        obs.write_status(
            self.home, {"last_error": "OSError", "last_error_at": 5.0}
        )

    def test_a_manual_ingest_keeps_the_error(self) -> None:
        code, _, _ = self.muninn("ingest")
        self.assertEqual(code, 0)
        status = obs.read_status(self.home)
        self.assertEqual(status["last_error"], "OSError")
        self.assertEqual(status["last_error_at"], 5.0)
        self.assertIn("last_pass_at", status)

    def test_a_failed_manual_ingest_keeps_it_too(self) -> None:
        with mock.patch.object(
            ingest, "run_pass", side_effect=store.BusyError
        ):
            code, out, _ = self.muninn("ingest")
        self.assertEqual((code, out["error"]), (3, "busy"))
        self.assertEqual(obs.read_status(self.home)["last_error"], "OSError")

    def test_the_pollers_good_pass_clears_it(self) -> None:
        heartbeat(self.home, PassStats(), self.env, clears_error=True)
        status = obs.read_status(self.home)
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["last_error_at"])


class StoppedNoteTests(CliCase):
    """The hook's "stopped with" note and the pass stamp it is judged by."""

    def notes(self, error_at: float | None, pass_at: float) -> str:
        """Return the hook notes for an error and a pass at given times."""
        obs.write_status(
            self.home,
            {
                "last_error": "OSError",
                "last_error_at": error_at,
                "last_pass_at": pass_at,
                "interval_s": 60,
            },
        )
        return " ".join(hook_notes.index_notes(self.home))

    def test_an_error_after_the_pass_is_noted(self) -> None:
        now = time.time()
        self.assertIn(STOPPED, self.notes(now, now - 5))

    def test_an_error_at_the_same_instant_is_noted(self) -> None:
        now = time.time()
        self.assertIn(STOPPED, self.notes(now, now))

    def test_an_error_before_the_pass_is_not_noted(self) -> None:
        now = time.time()
        self.assertNotIn(STOPPED, self.notes(now - 5, now))

    def test_an_error_with_no_time_is_noted(self) -> None:
        self.assertIn(STOPPED, self.notes(None, time.time()))


class CrashTests(CliCase):
    """A crash outside a pass leaves the same trace a failed pass does."""

    def crash(self) -> Result:
        """Run ``serve`` with a poller that raises; return its result."""
        with mock.patch.object(
            cli_serve._Poller, "run", side_effect=RuntimeError("SECRET")
        ):
            return cli_serve.serve(
                Namespace(interval=60.0), self.env, self.home, {}
            )

    def test_a_crash_records_the_error_in_the_status(self) -> None:
        self.assertEqual(self.crash(), (1, None))
        status = obs.read_status(self.home)
        self.assertEqual(status["last_error"], "RuntimeError")
        self.assertIsInstance(status["last_error_at"], float)

    def test_a_crash_survives_a_log_that_cannot_be_written(self) -> None:
        with mock.patch.object(obs, "log_poller", side_effect=OSError):
            result = self.crash()
        self.assertEqual(result, (1, None))
        # The unwritable log is what crashed ``prepare`` here, so that is the
        # recorded class; the point is that the handler itself did not raise.
        self.assertEqual(obs.read_status(self.home)["last_error"], "OSError")

    def test_a_crash_survives_a_status_that_cannot_be_written(self) -> None:
        with mock.patch.object(obs, "write_status", side_effect=OSError):
            self.assertEqual(self.crash(), (1, None))


class QuietFailureTests(CliCase):
    """When the streams cannot be silenced, the poller log says so."""

    def logged(self) -> str:
        """Return the poller log text."""
        return (self.home / "poller.log").read_text()

    def test_a_failed_redirect_is_logged_without_content(self) -> None:
        log = self.home / "poller.log"
        log.write_text("")
        with (
            mock.patch.object(os, "fstat", side_effect=lambda fd: log.stat()),
            mock.patch.object(os, "dup2", side_effect=PermissionError("x")),
        ):
            cli_serve._quiet_streams(self.home)
        text = self.logged()
        self.assertIn('"event":"quiet_failed"', text)
        self.assertIn('"exc":"PermissionError"', text)
        self.assertNotIn(str(self.home), text)

    def test_an_unopenable_null_device_is_logged(self) -> None:
        (self.home / "poller.log").write_text("")
        real = os.open

        def refuse(path: str | Path, flags: int, *rest: int) -> int:
            if str(path) == os.devnull:
                raise PermissionError("no")
            return real(path, flags, *rest)

        with mock.patch.object(os, "open", refuse):
            cli_serve._quiet_streams(self.home)
        self.assertIn("quiet_failed", self.logged())

    def test_no_log_file_is_not_a_failure(self) -> None:
        cli_serve._quiet_streams(self.home)
        self.assertFalse((self.home / "poller.log").exists())
