"""The ``stats`` report: counts only, never text.

Everything here is derived from aggregate queries and the heartbeat file,
so the report is safe to paste into a bug report.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from muninn import classify, store
from muninn.obs_status import (
    freshness,
    install_sha,
    poller_error,
    read_status,
)
from muninn.query.index_age import ALIVE_AT, seconds_since

_LAST_PASS_FIELDS = (
    "last_pass_at",
    "duration_s",
    "files_changed",
    "events_added",
    "skipped_files",
    "unreadable_files",
    "failed",
    "errors",
    "busy_skips",
)


def human_bytes(n: float) -> str:
    """Format a byte count in 1024-based units, such as ``188.5 MB``.

    Args:
        n: Number of bytes.

    Returns:
        The value with one of B, KB, MB, GB or TB.
    """
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def db_space(conn: sqlite3.Connection) -> dict[str, int | float]:
    """Measure database pages and how much of the file is free.

    Args:
        conn: Any open connection to the store.

    Returns:
        ``page_count``, ``freelist_count``, ``page_size`` and
        ``free_ratio`` (0.0 for an empty file).
    """
    pages, free, size = (
        conn.execute(f"PRAGMA {p}").fetchone()[0]
        for p in ("page_count", "freelist_count", "page_size")
    )
    return {
        "page_count": pages,
        "freelist_count": free,
        "page_size": size,
        "free_ratio": round(free / pages, 4) if pages else 0.0,
    }


def _hash_mismatches(home: Path) -> int:
    """Count open-time line hash failures recorded in the call logs."""
    bad = 0
    for name in ("calls.jsonl.1", "calls.jsonl"):
        try:
            lines = (home / name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                bad += json.loads(line).get("hash_ok") is False
            except (ValueError, AttributeError):
                continue  # a torn or foreign line is not a mismatch
    return bad


def _pairs(
    conn: sqlite3.Connection, sql: str, args: tuple[float, ...] = ()
) -> dict[str, int]:
    """Run a two-column query and return it as a mapping."""
    return {r[0]: r[1] for r in conn.execute(sql, args)}


def _flag_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count events carrying each flag bit."""
    flags = conn.execute(
        "SELECT total(flags & 1 > 0), total(flags & 2 > 0),"
        " total(flags & 4 > 0) FROM event"
    ).fetchone()
    names = ("marker", "redacted", "truncated")
    return dict(zip(names, (int(n) for n in flags), strict=True))


def _table_counts(conn: sqlite3.Connection) -> dict[str, object]:
    """Aggregate counts of sources, events, issues and knowledge."""
    return {
        "sources": {
            "/".join(r[:4]): r[4]
            for r in conn.execute(
                "SELECT provider, root, thread_class, status, count(*)"
                " FROM source GROUP BY 1, 2, 3, 4"
            )
        },
        "events": _pairs(conn, "SELECT kind, count(*) FROM event GROUP BY 1"),
        "events_by_provider": _pairs(
            conn,
            "SELECT s.provider, count(*) FROM event e"
            " JOIN source s ON s.id = e.source_id GROUP BY 1",
        ),
        "flags": _flag_counts(conn),
        "skipped_lines": int(
            conn.execute("SELECT total(skipped_lines) FROM source").fetchone()[
                0
            ]
        ),
        "issues": _pairs(
            conn, "SELECT code, count(*) FROM source_issue GROUP BY 1"
        ),
        # Format drift: such a thread is never guessed to be primary.
        "other_threads": _pairs(
            conn,
            "SELECT class_reason, count(*) FROM source"
            " WHERE thread_class = 'other' GROUP BY 1",
        ),
        # An expired entry is counted apart from current: expiry is derived.
        "knowledge": _pairs(
            conn,
            "SELECT CASE WHEN status = 'current' AND valid_until <= ?"
            " THEN 'expired' ELSE status END, count(*)"
            " FROM knowledge GROUP BY 1",
            (time.time(),),
        ),
        "knowledge_restricted": conn.execute(
            "SELECT count(*) FROM knowledge WHERE sensitivity = 'restricted'"
            " AND status = 'current'"
        ).fetchone()[0],
        "citations": _pairs(
            conn, "SELECT state, count(*) FROM citation GROUP BY 1"
        ),
        "tombstones": _pairs(
            conn, "SELECT level, count(*) FROM tombstone GROUP BY 1"
        ),
    }


def reread(conn: sqlite3.Connection) -> dict[str, int]:
    """Count the sources not yet read under the current classifier.

    After a release that changes classification the poller re-reads every
    source once; this says how far along that is. Missing sources are left
    out because their files are not there to read; one that returns is
    counted again. A source that fails, or is skipped for an unusable first
    line, is never re-stamped and stays pending.

    Args:
        conn: Read connection.

    Returns:
        ``pending``, the active sources still stamped with an older
        classifier version, and ``of``, all active sources.
    """
    pending, total = conn.execute(
        "SELECT total(classifier_version != ?), count(*) FROM source"
        " WHERE status = 'active'",
        (classify.CLASSIFIER_VERSION,),
    ).fetchone()
    return {"pending": int(pending), "of": int(total)}


def _alive_age(status: Mapping[str, object]) -> int | None:
    """Return the age of the alive stamp of a pass that is still running.

    Args:
        status: Parsed status.json.

    Returns:
        Seconds since a running pass last stamped alive, or None between
        passes and when no usable stamp exists (not written yet, failed to
        write, or from the future).
    """
    alive = seconds_since(status.get(ALIVE_AT), future_ok=False)
    last = seconds_since(status.get("last_pass_at"))
    # A stamp older than the last finished pass was left by a poller that
    # died mid-pass, not by a pass that is running now.
    if alive is None or (last is not None and alive >= last):
        return None
    return alive


def _usage(conn: sqlite3.Connection) -> dict[str, object]:
    """Per-session muninn call counts, and their per-provider totals."""
    rows = conn.execute(
        "SELECT provider, session_root, sum(calls), sum(errors),"
        " max(last_ts) FROM usage GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    names = ("provider", "session_root", "calls", "errors", "last_ts")
    totals: dict[str, dict[str, int]] = {}
    for provider, _, calls, errors, _ in rows:
        t = totals.setdefault(
            provider, {"calls": 0, "errors": 0, "sessions": 0}
        )
        t["calls"] += calls
        t["errors"] += errors
        t["sessions"] += 1
    return {
        "usage": [dict(zip(names, r, strict=True)) for r in rows],
        "usage_totals": totals,
    }


def stats(
    conn: sqlite3.Connection,
    home: Path,
    env: Mapping[str, str],
    *,
    usage: bool = False,
) -> dict[str, object]:
    """Build the counts-only ``stats`` report.

    Args:
        conn: Read connection.
        home: Data directory.
        env: Environment, for the installed code version.
        usage: Also include per-session muninn call counts.

    Returns:
        Sources, events, flags, issues, knowledge, tombstones and the last
        poller pass; never any text.
    """
    status = read_status(home)
    db = store.db_path(home)
    out = _table_counts(conn)
    out["db_bytes"] = db.stat().st_size if db.exists() else 0
    out["db_space"] = db_space(conn)
    out["last_pass"] = (
        {k: status.get(k) for k in _LAST_PASS_FIELDS}
        | freshness(status)
        | {"alive_age_s": _alive_age(status)}
        | {"last_error": poller_error(status)}
    )
    out["reread"] = reread(conn)
    out["install_sha"] = install_sha(env) or status.get("install_sha")
    out["classifier_version"] = classify.CLASSIFIER_VERSION
    out["hash_mismatches"] = _hash_mismatches(home)
    if usage:
        out |= _usage(conn)
    return out
