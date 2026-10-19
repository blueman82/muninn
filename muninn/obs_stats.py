"""The ``stats`` report: counts only, never text.

Everything here is derived from aggregate queries and the heartbeat file,
so the report is safe to paste into a bug report.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import cast

from muninn import classify, platform_io, store
from muninn.knowledge_read import read_snapshot
from muninn.obs_status import (
    ELIGIBLE,
    RECEIPTED,
    REVIEW_RECORDS,
    alive_age,
    freshness,
    install_sha,
    poller_error,
    read_status,
)
from muninn.review_models import ReviewStats, Usage

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


_MISSES = {
    "search": {
        "zero": "miss_zero",
        "outside": "miss_outside",
        "noterms": "miss_noterms",
    },
    "hook prompt": {
        "zero": "miss_zero",
        "outside": "miss_outside",
        "floor": "miss_floor",
    },
}


def _scan_calls(home: Path) -> tuple[int, dict[str, object]]:
    """Read both call logs once for hash failures and outcome counts.

    Args:
        home: Data directory.

    Returns:
        The count of open-time line hash failures and the
        ``retrieval_outcomes`` report. A torn or foreign line is neither a
        mismatch nor counted in ``window_lines``; a call without ``stages``
        counts in ``calls`` only.
    """
    bad = window = 0
    counts = {
        cmd: {"calls": 0} | dict.fromkeys(misses, 0)
        for cmd, misses in _MISSES.items()
    }
    for name in ("calls.jsonl.1", "calls.jsonl"):
        try:
            path = home / name
            lines = path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except (OSError, ValueError):
            continue
        for line in lines:
            try:
                call = json.loads(line)
                bad += call.get("hash_ok") is False
                cmd = call.get("cmd")
                raw: object = call.get("stages")
            except (ValueError, AttributeError):
                continue  # a torn or foreign line is not a mismatch
            window += 1
            stages: dict[str, object] = (
                cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
            )
            if isinstance(cmd, str) and cmd in counts:
                counts[cmd]["calls"] += 1
                for key, miss in _MISSES[cmd].items():
                    counts[cmd][key] += stages.get(miss) == 1
    return bad, {
        "window_lines": window,
        "search": counts["search"],
        "hook_prompt": counts["hook prompt"],
    }


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
        "knowledge": _pairs(
            conn, "SELECT state, count(*) FROM knowledge GROUP BY 1"
        ),
        "evidence": _pairs(
            conn, "SELECT state, count(*) FROM review_evidence GROUP BY 1"
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
    with read_snapshot(conn):
        out = _table_counts(conn)
        out["review"] = review_stats(conn, time.time()).model_dump(mode="json")
    out["db_bytes"] = db.stat().st_size if db.exists() else 0
    out["db_space"] = db_space(conn)
    out["last_pass"] = (
        {k: status.get(k) for k in _LAST_PASS_FIELDS}
        | freshness(status)
        | {"alive_age_s": alive_age(status)}
        | {"last_error": poller_error(status)}
    )
    out["reread"] = reread(conn)
    out["install_sha"] = install_sha(env) or status.get("install_sha")
    out["classifier_version"] = classify.CLASSIFIER_VERSION
    out["hash_mismatches"], out["retrieval_outcomes"] = _scan_calls(home)
    if usage:
        out |= _usage(conn)
    return out


def review_records(conn: sqlite3.Connection) -> tuple[int, int]:
    """Count indexed logical records outside the activation archive.

    Args:
        conn: Read connection in the caller's coherent snapshot.

    Returns:
        Eligible and receipted record counts, including explicit replay.
    """
    return tuple(
        conn.execute(
            "SELECT count(*)" + REVIEW_RECORDS + "WHERE " + condition
        ).fetchone()[0]
        for condition in (ELIGIBLE, RECEIPTED)
    )


def _review_usage(conn: sqlite3.Connection) -> Usage:
    """Sum reported calls once, retaining unknown components and currency.

    Args:
        conn: Read connection in the caller's coherent snapshot.

    Returns:
        Nullable aggregate usage; reservations are never actual charges.
    """
    rows = [
        _usage_value(row[1]) if row[0] == "completed" else None
        for row in conn.execute("SELECT state,usage_json FROM review_call")
    ]
    tokens: dict[str, int | None] = {}
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        values = [
            getattr(row, name) if row is not None else None for row in rows
        ]
        tokens[name] = (
            sum(value for value in values if value is not None)
            if values and all(value is not None for value in values)
            else None
        )
    currencies = {row.currency if row is not None else None for row in rows}
    known = bool(rows) and None not in currencies and len(currencies) == 1
    costs = [row.cost_decimal if row is not None else None for row in rows]
    cost = (
        str(
            sum(
                (Decimal(value) for value in costs if value is not None),
                Decimal(0),
            )
        )
        if known and all(value is not None for value in costs)
        else None
    )
    return Usage.model_validate(
        {
            **tokens,
            "cost_decimal": cost,
            "currency": next(iter(currencies)) if known else None,
        }
    )


def _observed_busy(conn: sqlite3.Connection) -> int | None:
    """Count retained busy acknowledgments with incomplete coverage."""
    filename = conn.execute("PRAGMA database_list").fetchone()[2]
    if not filename:
        return None
    count, observed = 0, False
    for name in ("calls.jsonl.1", "calls.jsonl"):
        try:
            with platform_io.open_regular(
                Path(filename).parent / name, root=Path(filename).parent
            ) as handle:
                platform_io.assert_private_fd(handle.fileno())
                for line in handle:
                    try:
                        row: object = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        observed = True
                        count += (
                            cast("dict[str, object]", row).get("event")
                            == "review_enqueue_busy"
                        )
        except (OSError, ValueError):
            continue
    return count if observed else None


def review_stats(conn: sqlite3.Connection, now: float) -> ReviewStats:
    """Read durable review counts and explicitly incomplete busy observations.

    Args:
        conn: Current normalized store connection; no writer is acquired.
        now: Observation time in UTC seconds.

    Returns:
        Content-free counts, nonnegative ages and nullable actual usage.
    """
    with read_snapshot(conn):
        pending = conn.execute(
            "SELECT min(created_at) FROM review_request "
            "WHERE state IN ('pending','waiting')"
        ).fetchone()[0]
        progress = conn.execute(
            "SELECT max(progress_at) FROM review_attempt"
        ).fetchone()[0]
        eligible, completed = review_records(conn)
        return ReviewStats.model_validate(
            {
                "schema_version": 1,
                "requests_by_state": _pairs(
                    conn,
                    "SELECT state,count(*) FROM review_request GROUP BY state",
                ),
                "calls_by_state": _pairs(
                    conn,
                    "SELECT state,count(*) FROM review_call GROUP BY state",
                ),
                "oldest_pending_age_seconds": (
                    None if pending is None else max(0.0, now - pending)
                ),
                "last_progress_age_seconds": (
                    None if progress is None else max(0.0, now - progress)
                ),
                "eligible_records": eligible,
                "completed_records": completed,
                "unresolved_items": conn.execute(
                    "SELECT count(*) FROM unresolved WHERE state='open'"
                ).fetchone()[0],
                "usage": _review_usage(conn),
                "observed_enqueue_busy": _observed_busy(conn),
                "busy_count_complete": False,
            }
        )


def _usage_value(raw: str | None) -> Usage | None:
    """Treat unavailable or malformed usage as unknown, never as zero."""
    try:
        usage = None if raw is None else Usage.model_validate_json(raw)
        if (
            usage is not None
            and usage.currency is not None
            and not re.fullmatch(r"[A-Z]{3}", usage.currency)
        ):
            usage = usage.model_copy(
                update={"currency": None, "cost_decimal": None}
            )
        return usage
    except ValueError:
        return None
