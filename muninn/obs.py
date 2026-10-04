"""muninn observability: the call log, the heartbeat, stats and doctor.

The logging, heartbeat and stats code lives in ``obs_log``, ``obs_status``
and ``obs_stats``; this module re-exports it and owns ``doctor``, whose
health checks read the data directory, the store and the launchd job.
``run`` and the doctor thresholds stay here because tests replace them on
this module.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from muninn import ingest, store
from muninn.obs_log import ROTATE_BYTES, actor, log_call, log_poller
from muninn.obs_stats import db_space, human_bytes, reread, stats
from muninn.obs_status import (
    count_field,
    freshness,
    install_sha,
    poller_error,
    read_status,
    write_status,
)

__all__ = [
    "ROTATE_BYTES",
    "CheckResult",
    "DoctorReport",
    "actor",
    "count_field",
    "db_space",
    "doctor",
    "freshness",
    "human_bytes",
    "install_sha",
    "log_call",
    "log_poller",
    "poller_error",
    "read_status",
    "run",
    "stats",
    "write_status",
]

# ponytail: untuned guesses; retune from real colleague databases.
DB_WARN_BYTES = 2 * 1024**3
FREE_WARN_RATIO = 0.25
FREE_WARN_BYTES = 64 * 1024**2

LABEL = "com.muninn"
# Everything muninn itself puts in the data directory; anything else is
# reported by ``unexpected_files``.
DATA_FILES = frozenset(
    {
        "muninn.sqlite",
        "writer.lock",
        "status.json",
        "calls.jsonl",
        "calls.jsonl.1",
        "poller.log",
        "poller.log.1",
        "tombstones.jsonl",
        "tombstone.key",
        "recall.off",
    }
)


class CheckResult(TypedDict):
    """One doctor check as it appears in the JSON output."""

    check: str
    ok: bool | None
    level: str
    detail: str


class DoctorReport(TypedDict):
    """The doctor output: overall verdict and every check."""

    ok: bool
    checks: list[CheckResult]


class JobInfo(TypedDict):
    """The launchd job: its process id (if running) and command line."""

    pid: int | None
    cmd: str


def run(argv: Sequence[object]) -> subprocess.CompletedProcess[bytes]:
    """Run launchctl or ps for doctor.

    Tests replace this function so they never touch the real launchd
    domain.

    Args:
        argv: Command and arguments; each is converted with ``str``.

    Returns:
        The completed process; output is captured, never decoded.
    """
    return subprocess.run(
        [str(a) for a in argv], capture_output=True, timeout=10, check=False
    )


def _result(
    name: str,
    ok: object,
    detail: object = "",
    level: str = "error",
) -> CheckResult:
    """Build a check result; ``ok=None`` means "could not tell"."""
    return {
        "check": name,
        "ok": None if ok is None else bool(ok),
        "level": level,
        "detail": str(detail),
    }


def _mode(path: Path) -> int:
    """Return the permission bits of a path."""
    return path.stat().st_mode & 0o777


def _names(home: Path) -> list[str]:
    """Sorted names in the data directory; empty if it is absent."""
    return sorted(p.name for p in home.iterdir()) if home.is_dir() else []


def _lock_free(home: Path) -> bool:
    """Whether the writer lock is free right now (never waits)."""
    try:
        with store.writer_lock(home, wait_s=0):
            return True
    except (store.BusyError, OSError):
        return False


def _job() -> JobInfo | None:
    """Return the launchd job's pid and command, or None if not loaded."""
    r = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    if r.returncode:
        return None
    found = re.search(rb"^\s*pid = (\d+)\s*$", r.stdout, re.MULTILINE)
    if not found:
        return {"pid": None, "cmd": ""}
    ps = run(["ps", "-ww", "-o", "command=", "-p", found[1].decode()])
    return {
        "pid": int(found[1]),
        "cmd": ps.stdout.decode(errors="replace").strip(),
    }


def _writer_secure_delete() -> bool:
    """Whether writers turn ``secure_delete`` on.

    It is a per-connection setting, not stored in the file, so the writer
    pragmas are applied to an in-memory connection. Opening the real store
    would roll back a hot journal that doctor is meant to report.

    Returns:
        True when ``secure_delete`` reads back as on.
    """
    conn = sqlite3.connect(":memory:")
    try:
        for pragma in store.WRITER_PRAGMAS:
            conn.execute(pragma)
        return conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()


def _data_dir_mode(home: Path) -> CheckResult:
    """The data directory exists and is private (0700)."""
    present = home.is_dir()
    return _result(
        "data_dir_mode",
        present and _mode(home) == 0o700,
        oct(_mode(home)) if present else "absent",
    )


def _file_modes(home: Path) -> CheckResult:
    """No data file is readable by group or others."""
    loose = [
        n
        for n in _names(home)
        if (home / n).is_file() and _mode(home / n) & 0o077
    ]
    return _result("file_modes", not loose, ",".join(loose))


def _unexpected_files(home: Path) -> CheckResult:
    """The data directory holds only files muninn writes."""
    stray = [
        n
        for n in _names(home)
        if n not in DATA_FILES
        and n != "muninn.sqlite-journal"
        and not n.startswith(store.UNREADABLE_PREFIX)
        and not n.startswith(".status.json.")  # an atomic write in flight
    ]
    return _result("unexpected_files", not stray, ",".join(stray))


def _unowned_journal(home: Path) -> CheckResult:
    """No rollback journal is left behind by a crashed writer."""
    journal = (home / "muninn.sqlite-journal").exists()
    return _result(
        "unowned_journal",
        not (journal and _lock_free(home)),
        "a crashed writer's journal: the next writer rolls it back",
    )


def _journal_mode(conn: sqlite3.Connection) -> CheckResult:
    """The store uses the rollback journal, not WAL."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return _result("journal_mode", mode == "delete", mode)


def _fts_secure_delete(conn: sqlite3.Connection) -> CheckResult:
    """Both FTS indexes zero deleted content."""
    rows = [
        conn.execute(
            f"SELECT v FROM {t}_config WHERE k = 'secure-delete'"
        ).fetchone()
        for t in ("event_fts", "knowledge_fts")
    ]
    return _result("fts_secure_delete", all(r and r[0] == 1 for r in rows))


def _quick_check(conn: sqlite3.Connection) -> CheckResult:
    """SQLite's quick integrity check passes."""
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    return _result("quick_check", quick == "ok", quick[:80])


def _count(conn: sqlite3.Connection, sql: str) -> int:
    """Run a single-count query."""
    return conn.execute(sql).fetchone()[0]


def _missing_sources(conn: sqlite3.Connection) -> CheckResult:
    """Report transcripts that vanished (informational)."""
    missing = _count(
        conn, "SELECT count(*) FROM source WHERE status = 'missing'"
    )
    return _result("missing_sources", True, missing, level="info")


def _other_threads(conn: sqlite3.Connection) -> CheckResult:
    """Report threads of an unrecognised format (informational)."""
    drift = _count(
        conn, "SELECT count(*) FROM source WHERE thread_class = 'other'"
    )
    return _result("other_threads", True, drift, level="info")


def _reread(conn: sqlite3.Connection) -> CheckResult:
    """Say how far a classifier change's one-time re-read has got."""
    got = reread(conn)
    return _result(
        "reread", True, f"{got['pending']} of {got['of']}", level="info"
    )


def _citations_resolve(conn: sqlite3.Connection) -> CheckResult:
    """Every live citation still points at an event with the same hash."""
    broken = _count(
        conn,
        "SELECT count(*) FROM citation c WHERE c.state = 'live'"
        " AND NOT EXISTS (SELECT 1 FROM event e JOIN source s"
        " ON s.id = e.source_id WHERE s.provider = c.provider AND"
        " s.thread_id = c.thread_id AND e.line = c.line AND"
        " e.part = c.part AND e.line_sha256 = c.line_sha256)",
    )
    return _result("citations_resolve", not broken, broken, level="warn")


def _db_size(conn: sqlite3.Connection) -> CheckResult:
    """The database is below the size warning threshold."""
    space = db_space(conn)
    size = space["page_count"] * space["page_size"]
    return _result(
        "db_size",
        size < DB_WARN_BYTES,
        f"{human_bytes(size)}; threshold {human_bytes(DB_WARN_BYTES)}",
        level="warn",
    )


def _db_free_space(conn: sqlite3.Connection) -> CheckResult:
    """Free pages are not a large share of the file."""
    space = db_space(conn)
    free = space["freelist_count"] * space["page_size"]
    # Warn only above both a ratio and an absolute size, so a small file
    # with a high free share does not trigger it.
    wasteful = space["free_ratio"] > FREE_WARN_RATIO and free > FREE_WARN_BYTES
    return _result(
        "db_free_space",
        not wasteful,
        f"{human_bytes(free)} free ({space['free_ratio']:.0%});"
        " run: muninn compact",
        level="warn",
    )


# Order is the order of the checks in the doctor output.
_STORE_CHECKS: tuple[Callable[[sqlite3.Connection], CheckResult], ...] = (
    _journal_mode,
    _fts_secure_delete,
    _quick_check,
    _missing_sources,
    _other_threads,
    _reread,
    _citations_resolve,
    _db_size,
    _db_free_space,
)


def _store_checks(home: Path) -> list[CheckResult]:
    """Open the store read-only and run every store check."""
    try:
        conn = store.connect_ro(store.db_path(home))
    except store.StoreUnavailableError as exc:
        return [_result("store_readable", False, type(exc).__name__)]
    results = [_result("store_readable", True)]
    try:
        # extend() appends as the generator yields, so the checks that ran
        # before an SQLite error are kept.
        results.extend(check(conn) for check in _STORE_CHECKS)
    except sqlite3.Error as exc:
        results.append(_result("store_readable", False, type(exc).__name__))
    finally:
        conn.close()
    return results


def _writer_pragmas() -> CheckResult:
    """Writers will delete securely."""
    return _result("writer_secure_delete", _writer_secure_delete())


def _heartbeat(home: Path) -> CheckResult:
    """The poller reported recently."""
    fresh = freshness(read_status(home))
    return _result("heartbeat", fresh["poller"] == "ok", fresh["index_age_s"])


def _failed_sources(home: Path) -> CheckResult:
    """The last poller pass failed on no source."""
    failed = read_status(home).get("failed") or 0
    return _result("failed_sources", not failed, failed, level="warn")


def _poller_error(home: Path) -> CheckResult:
    """The poller's last pass did not end in an exception."""
    err = poller_error(read_status(home))
    return _result("poller_error", err is None, err or "", level="warn")


def _unreadable_files(home: Path) -> CheckResult:
    """The last pass could open every transcript file it listed."""
    n = count_field(read_status(home), "unreadable_files")
    return _result("unreadable_files", not n, n, level="warn")


def _aside_files(home: Path) -> CheckResult:
    """No unreadable store set aside by a rebuild is still on disk."""
    # erase cannot scrub these, so they keep erased text until removed.
    kept = [n for n in _names(home) if n.startswith(store.UNREADABLE_PREFIX)]
    return _result("aside_files", not kept, ",".join(kept), level="warn")


def _launchd_job() -> CheckResult:
    """The launchd job is loaded and running."""
    job = _job()
    if job is None:
        return _result("launchd_job", False, None)
    # Loaded but with no pid yet is "unknown" (ok=None), not a failure.
    pid = job["pid"]
    return _result("launchd_job", None if pid is None else bool(pid), pid)


def _roots_readable(env: Mapping[str, str]) -> CheckResult:
    """Every provider transcript root that exists can be read."""
    blocked = [
        n
        for n, p in ingest.default_roots(env).items()
        if p.exists() and not os.access(p, os.R_OK | os.X_OK)
    ]
    return _result("roots_readable", not blocked, ",".join(blocked))


def _roots_present(env: Mapping[str, str]) -> CheckResult:
    """Report provider roots that do not exist (informational)."""
    absent = [
        n for n, p in ingest.default_roots(env).items() if not p.exists()
    ]
    return _result("roots_present", True, ",".join(absent), level="info")


# Order is the order of the checks in the doctor output.
_DIRECTORY_CHECKS: tuple[Callable[[Path], CheckResult], ...] = (
    _data_dir_mode,
    _file_modes,
    _unexpected_files,
    _unowned_journal,
)


def doctor(home: Path, env: Mapping[str, str]) -> DoctorReport:
    """Run the health checks.

    Only ``error`` level checks decide ``ok``; ``warn`` and ``info`` never
    do. A missing provider source is information, not an error.

    Args:
        home: Data directory.
        env: Environment, for provider roots and ``HOME``.

    Returns:
        ``ok`` and the ordered list of check results.
    """
    checks = [check(home) for check in _DIRECTORY_CHECKS]
    checks += _store_checks(home)
    checks += [
        _writer_pragmas(),
        _heartbeat(home),
        _failed_sources(home),
        _poller_error(home),
        _unreadable_files(home),
        _aside_files(home),
        _launchd_job(),
        _roots_readable(env),
        _roots_present(env),
    ]
    ok = all(c["ok"] is not False for c in checks if c["level"] == "error")
    return {"ok": ok, "checks": checks}
