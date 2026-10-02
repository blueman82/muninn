"""The poller says it is alive inside a long pass, and only inside one."""

from __future__ import annotations

import json
import os
import signal
import time
import unittest
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli_serve, ingest, obs, store
from muninn.query.index_age import ALIVE_AT
from tests.cli_support import CliCase

LONG_AGO = 900  # seconds since the last finished pass: stale on its own
# Taken before any patching, for the stand-ins that must still write.
REAL_WRITE_STATUS = obs.write_status


class ServeAliveTests(CliCase):
    """Run the real ``serve`` for one pass with ``ingest`` replaced."""

    def setUp(self) -> None:
        super().setUp()
        obs.write_status(
            self.home,
            {"last_pass_at": time.time() - LONG_AGO, "interval_s": 60},
        )
        self.clock = [0.0]
        self.alive_writes: list[Any] = []

    def status(self) -> dict[str, object]:
        """Return the current status.json."""
        return obs.read_status(self.home)

    def spy_status(self, fields: dict[str, object]) -> None:
        """Write status for real, remembering alive stamps being written."""
        if fields.get(ALIVE_AT) is not None:
            self.alive_writes.append(fields[ALIVE_AT])
        REAL_WRITE_STATUS(self.home, fields)

    def keep_signal_handlers(self) -> None:
        """Put the process's SIGTERM and SIGHUP handlers back after the test.

        ``serve`` installs its own and this test process outlives it.
        """
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def serve_one_pass(
        self,
        fake: Callable[..., ingest.PassStats],
        *,
        write_status: Callable[[Any, dict[str, object]], None] | None = None,
    ) -> None:
        """Run ``serve`` until its first sleep, with ``ingest`` replaced.

        Args:
            fake: Stands in for ``ingest.ingest``; gets ``on_source``.
            write_status: Stands in for ``obs.write_status`` (called with the
                home and the fields); defaults to the real one.
        """
        self.keep_signal_handlers()
        spy = write_status or (lambda _home, fields: self.spy_status(fields))
        with (
            mock.patch.object(cli_serve.ingest, "ingest", fake),
            mock.patch.object(cli_serve.obs, "write_status", spy),
            mock.patch.object(
                cli_serve.time, "sleep", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(
                cli_serve.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            cli_serve.serve(Namespace(interval=60.0), self.env, self.home, {})

    def test_a_stop_arriving_just_after_a_status_write_still_stops(
        self,
    ) -> None:
        """SIGTERM right after the rename must stop the poller, not pass."""
        real_replace = Path.replace
        fired: list[int] = []

        def replace_then_sigterm(src: Path, dst: Path) -> Path:
            moved = real_replace(src, dst)
            if not fired and dst.name == "status.json":
                fired.append(1)
                os.kill(os.getpid(), signal.SIGTERM)  # the poller's handler
            return moved

        def fake(
            conn: object,
            roots: object,
            *,
            on_source: Callable[[], None],
            **_: Any,
        ) -> ingest.PassStats:
            on_source()
            return ingest.PassStats()

        self.keep_signal_handlers()
        with (
            mock.patch.object(cli_serve.ingest, "ingest", fake),
            mock.patch.object(Path, "replace", replace_then_sigterm),
            mock.patch.object(
                cli_serve.time,
                "sleep",
                side_effect=AssertionError("kept running after the stop"),
            ),
        ):
            result = cli_serve.serve(
                Namespace(interval=60.0), self.env, self.home, {}
            )
        self.assertEqual(result, (0, None))
        self.assertEqual(fired, [1])

    def test_a_stop_noted_mid_transaction_survives_a_silent_rollback(
        self,
    ) -> None:
        """No COMMIT or ROLLBACK statement ever consumes the wrapper's flag."""

        def fake(conn: Any, roots: object, **_: Any) -> ingest.PassStats:
            conn.execute("BEGIN IMMEDIATE")
            os.kill(os.getpid(), signal.SIGTERM)  # handler sees the txn
            conn.rollback()  # ends it without going through execute()
            return ingest.PassStats()

        self.keep_signal_handlers()
        with (
            mock.patch.object(cli_serve.ingest, "ingest", fake),
            mock.patch.object(
                cli_serve.time,
                "sleep",
                side_effect=AssertionError("kept running after the stop"),
            ),
        ):
            result = cli_serve.serve(
                Namespace(interval=60.0), self.env, self.home, {}
            )
        self.assertEqual(result, (0, None))

    def test_a_running_pass_reads_ok_and_the_stamp_goes_when_it_ends(
        self,
    ) -> None:
        during: list[dict[str, object]] = []

        def fake(
            conn: object,
            roots: object,
            *,
            on_source: Callable[[], None],
            **_: Any,
        ) -> ingest.PassStats:
            on_source()
            during.append(obs.freshness(self.status()))
            return ingest.PassStats()

        self.serve_one_pass(fake)
        self.assertEqual(during[0]["poller"], "ok")
        # The index is still as old as the last finished pass.
        self.assertGreaterEqual(int(str(during[0]["index_age_s"])), LONG_AGO)
        self.assertIsNone(self.status()[ALIVE_AT])

    def test_the_stamp_is_written_at_most_every_few_seconds(self) -> None:
        def fake(
            conn: object,
            roots: object,
            *,
            on_source: Callable[[], None],
            **_: Any,
        ) -> ingest.PassStats:
            for now in (0.0, 1.0, 4.9, 5.1):
                self.clock[0] = now
                on_source()
            return ingest.PassStats()

        self.serve_one_pass(fake)
        self.assertEqual(len(self.alive_writes), 2)  # first call, then 5.1

    def test_a_failed_stamp_write_is_logged_once_and_does_not_abort(
        self,
    ) -> None:
        def broken(home: Any, fields: dict[str, object]) -> None:
            if fields.get(ALIVE_AT) is not None:
                raise PermissionError("denied")
            REAL_WRITE_STATUS(home, fields)

        def fake(
            conn: object,
            roots: object,
            *,
            on_source: Callable[[], None],
            **_: Any,
        ) -> ingest.PassStats:
            for now in (0.0, 6.0, 12.0):  # three attempts past the throttle
                self.clock[0] = now
                on_source()
            return ingest.PassStats()

        self.serve_one_pass(fake, write_status=broken)
        lines = (self.home / "poller.log").read_text().splitlines()
        failures = [json.loads(x) for x in lines if "heartbeat_failed" in x]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["exc"], "PermissionError")
        self.assertNotIn("denied", "\n".join(lines))  # class name only
        self.assertEqual(self.status()["passes"], 1)

    def test_a_pass_that_keeps_failing_never_reads_ok(self) -> None:
        def fake(
            conn: object,
            roots: object,
            *,
            on_source: Callable[[], None],
            **_: Any,
        ) -> ingest.PassStats:
            on_source()
            raise RuntimeError("boom")

        self.serve_one_pass(fake)
        self.assertEqual(self.status()["last_error"], "RuntimeError")
        self.assertIsNone(self.status()[ALIVE_AT])
        self.assertEqual(obs.freshness(self.status())["poller"], "stale")

    def test_a_busy_lock_earns_no_stamp(self) -> None:
        def fake(*_: Any, **__: Any) -> ingest.PassStats:
            raise AssertionError("must not run while the lock is busy")

        with mock.patch.object(
            store, "writer_lock", side_effect=store.BusyError("held")
        ):
            self.serve_one_pass(fake)
        self.assertEqual(self.alive_writes, [])
        self.assertEqual(obs.freshness(self.status())["poller"], "stale")


if __name__ == "__main__":
    unittest.main()
