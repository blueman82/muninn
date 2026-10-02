"""``muninn serve``: the KeepAlive poller that launchd runs.

Each loop is a pass, a heartbeat and a sleep.  A pass is skipped, not
waited for, while another writer holds the lock; a human writer can still
wait for a running pass, up to ``WRITER_WAIT_S``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import time
from argparse import Namespace
from pathlib import Path
from types import FrameType
from typing import Any, cast

from muninn import ingest, obs, store
from muninn.cli_core import Env, Record, Result
from muninn.cli_maint import heartbeat
from muninn.query.index_age import ALIVE_AT

__all__ = ["serve"]

# While a pass runs, say so at most this often. A pass after a classifier
# change re-reads every transcript and takes minutes; without this the
# poller looks dead to the installer and to doctor for all of that time.
ALIVE_EVERY_S = 5.0


class _ServeStopError(BaseException):
    """SIGTERM or SIGHUP: leave serve once no transaction is open.

    A ``BaseException`` so the poller's ``except Exception`` error handler
    cannot swallow a stop request.
    """


class _StopAfterCommit:
    """The poller's connection, able to stop right after a transaction ends.

    Once ``stop`` is set mid-transaction, the next COMMIT or ROLLBACK raises
    ``_ServeStopError``.  Ingest commits each source in its own transaction,
    so the poller never leaves a journal behind.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap ``conn``; no stop is pending yet."""
        self._conn = conn
        self.stop = False

    def __getattr__(self, name: str) -> Any:
        """Delegate everything but ``execute`` to the real connection."""
        return getattr(self._conn, name)

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        """Execute ``sql``, then stop if one was requested and it ended."""
        cursor = self._conn.execute(sql, *args)
        if self.stop and sql in ("COMMIT", "ROLLBACK"):
            raise _ServeStopError
        return cursor


class _Poller:
    """State of one ``serve`` process: counters and the live connection."""

    def __init__(self, home: Path, env: Env, interval: float) -> None:
        """Resolve the provider roots and start the counters at zero."""
        self.home = home
        self.env = env
        self.interval = interval
        self.roots = ingest.default_roots(env)
        self.conn: _StopAfterCommit | None = None
        self.passes = self.skipped = self.quiet = 0
        self.idle_every = 1
        self.alive_at = float("-inf")  # monotonic time of the last write

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        """Stop now if idle, else after the open transaction ends."""
        conn = self.conn
        try:
            busy = conn is not None and conn.in_transaction
        except sqlite3.ProgrammingError:  # already closed
            busy = False
        if conn is not None and busy:
            conn.stop = True  # finish this source's transaction first
        else:
            raise _ServeStopError

    def prepare(self) -> None:
        """Tighten the log mode, install signal handlers, log the start."""
        # launchd opens StandardOutPath before Umask applies, leaving it
        # 0644; the log must be owner-only.
        with contextlib.suppress(FileNotFoundError):  # not run by launchd
            (self.home / "poller.log").chmod(0o600)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        # Hourly "idle" line, so a silent log still shows the poller alive.
        self.idle_every = max(1, round(3600 / self.interval))
        obs.log_poller(
            self.home,
            {
                "event": "start",
                "pid": os.getpid(),
                "interval_s": self.interval,
            },
        )

    def _alive(self) -> None:
        """Record that the poller is alive, at most every ``ALIVE_EVERY_S``."""
        now = time.monotonic()
        if now - self.alive_at < ALIVE_EVERY_S:
            return
        self.alive_at = now
        # A heartbeat that cannot be written must not abort the pass.
        with contextlib.suppress(OSError):
            obs.write_status(
                self.home, {ALIVE_AT: time.time(), "pid": os.getpid()}
            )

    def _ingest(self) -> ingest.PassStats:
        """Run one ingest pass under the writer lock, never waiting for it."""
        self._alive()
        with store.writer_lock(self.home, wait_s=0):
            raw = store.connect_rw(store.db_path(self.home))
            # The proxy only adds a stop hook around COMMIT/ROLLBACK and
            # forwards every other attribute, so it stands in for the
            # connection.
            self.conn = _StopAfterCommit(raw)
            try:
                return ingest.ingest(
                    cast(sqlite3.Connection, self.conn),
                    self.roots,
                    on_source=self._alive,
                )
            finally:
                raw.close()
                self.conn = None

    def _record_pass(self, stats: ingest.PassStats) -> None:
        """Write the heartbeat and, on news or once an hour, a log line."""
        self.passes += 1
        heartbeat(
            self.home,
            stats,
            self.env,
            pid=os.getpid(),
            interval_s=self.interval,
            passes=self.passes,
            busy_skips=self.skipped,
        )
        news = bool(stats.files_changed or stats.failed)
        self.quiet = 0 if news else self.quiet + 1
        if news or self.quiet % self.idle_every == 0:
            obs.log_poller(
                self.home,
                {
                    "event": "pass" if news else "idle",
                    "passes": self.passes,
                    "busy_skips": self.skipped,
                    "files_changed": stats.files_changed,
                    "events_added": stats.events_added,
                    "failed": stats.failed,
                    "duration_s": round(stats.duration_s, 3),
                    "counts": stats.errors,
                },
            )

    def step(self) -> None:
        """Run one pass; busy and error outcomes are recorded, not raised."""
        try:
            self._record_pass(self._ingest())
        except store.BusyError:
            self.skipped += 1
            obs.write_status(self.home, {"busy_skips": self.skipped})
        except Exception as exc:
            # Class name only: an exception message can quote transcript
            # text or paths, and poller.log must stay free of both.
            name = type(exc).__name__
            obs.log_poller(self.home, {"event": "error", "exc": name})
            obs.write_status(self.home, {"last_error": name})

    def run(self) -> Result:
        """Loop until a stop signal arrives; exit 0 with no output."""
        try:
            while True:
                began = time.monotonic()
                self.step()
                # Sleep the rest of the interval so a slow pass does not
                # stretch the cadence.
                time.sleep(
                    max(0.0, self.interval - (time.monotonic() - began))
                )
        except _ServeStopError:
            obs.log_poller(self.home, {"event": "stop", "passes": self.passes})
            return 0, None


def serve(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run the poller until SIGTERM or SIGHUP.

    Args:
        a: Parsed arguments; ``interval`` is seconds between passes.
        env: Process environment, for the provider roots.
        home: Data directory.
        record: Call-log entry (unused; serve logs to ``poller.log``).

    Returns:
        Exit 0 and no output once stopped.
    """
    poller = _Poller(home, env, a.interval)
    poller.prepare()
    return poller.run()
