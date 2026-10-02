"""pctx observability (design 7; spec O8d, O9, A5, A14).

calls.jsonl gets one allowlisted line per CLI call (no text, no query
string), rotated at 1 MiB x 2.  status.json is the poller heartbeat
(counts only), written atomically.  stats and doctor read the store.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from pctx import classify, ingest, query, store

ROTATE_BYTES = 1024 * 1024
LINE_BYTES = 1024
_NUMBERS = (
    "at",
    "scope_id",
    "n_terms",
    "bytes_out",
    "ms",
    "exit",
    "target_id",
    "n_context",
    "pid",
    "passes",
    "busy_skips",
    "interval_s",
    "files_changed",
    "events_added",
    "failed",
    "duration_s",
)
_FLAGS = ("hash_ok", "logged")
_ID_LISTS = ("returned_ids", "knowledge_ids", "ids")
_COUNTS = ("stages", "counts")
_CODES = {  # string fields: fixed shapes that cannot carry text
    "cmd": re.compile(r"[a-z][a-z-]{0,30}(?: [a-z-]{1,20})?"),
    "actor": re.compile(r"user|(?:claude|codex):[\w-]{1,12}"),
    "query_sha12": re.compile(r"[0-9a-f]{12}"),
    "error": re.compile(r"[a-z_]{1,40}"),
    "event": re.compile(r"[a-z_]{1,20}"),
    "exc": re.compile(r"[A-Za-z_]{1,60}"),  # an exception class name
}
_KEY = re.compile(r"[a-z_]{1,30}")


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean(record: Mapping) -> dict:
    """Only allowlisted fields of allowlisted shapes survive."""
    out = {}
    for key, value in record.items():
        if key in _NUMBERS and (_int(value) or isinstance(value, float)):
            out[key] = value
        elif key in _FLAGS and (value is None or isinstance(value, bool)):
            out[key] = value
        elif key in _CODES and isinstance(value, str):
            if _CODES[key].fullmatch(value):
                out[key] = value
        elif key in _ID_LISTS and isinstance(value, list):
            out[key] = [v for v in value if _int(v)]
        elif key in _COUNTS and isinstance(value, Mapping):
            out[key] = {
                k: v
                for k, v in value.items()
                if isinstance(k, str) and _KEY.fullmatch(k) and _int(v)
            }
    return out


def log_call(
    home: Path, record: Mapping, env: Mapping[str, str] = os.environ
) -> bool:
    """Append one line to calls.jsonl; False when disabled or denied (the
    response then says logged:false).  PCTX_NO_CALLLOG=1 disables it."""
    if env.get("PCTX_NO_CALLLOG") == "1":
        return False
    line = _clean({**record, "at": round(time.time(), 3)})

    def encoded():
        text = json.dumps(line, sort_keys=True, separators=(",", ":"))
        return text.encode() + b"\n"

    data = encoded()
    for key in _ID_LISTS:  # keep the line within LINE_BYTES
        while len(data) > LINE_BYTES and line.get(key):
            line[key] = line[key][: len(line[key]) // 2]
            data = encoded()
    return _append(home, "calls.jsonl", data)


def _append(home: Path, name: str, data: bytes) -> bool:
    """Append to home/name, rotating to name.1 at ROTATE_BYTES (two files
    at most); False when the append is denied."""
    path = home / name
    try:
        if path.exists() and path.stat().st_size + len(data) > ROTATE_BYTES:
            os.replace(path, home / f"{name}.1")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:  # e.g. the Codex sandbox denies the append
        return False
    return True


def log_poller(home: Path, record: Mapping) -> bool:
    """One allowlisted JSON line in poller.log: an event code, counts and an
    exception class name; never transcript text.  Rotated like calls.jsonl."""
    line = _clean({**record, "at": round(time.time(), 3)})
    data = json.dumps(line, sort_keys=True, separators=(",", ":")).encode()
    return _append(home, "poller.log", data + b"\n")


def actor(env: Mapping[str, str]) -> str:
    """claude:<12> or codex:<12> of the calling session, else user."""
    for name, provider in (
        ("CLAUDE_CODE_SESSION_ID", "claude"),
        ("CODEX_SESSION_ID", "codex"),
        ("CODEX_THREAD_ID", "codex"),
    ):
        if env.get(name):
            ident = re.sub(r"[^\w-]", "", env[name])[:12]
            return f"{provider}:{ident or 'unknown'}"
    return "user"


def read_status(home: Path) -> dict:
    """The parsed status.json, or {} when absent or unreadable."""
    try:
        data = json.loads((home / "status.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(home: Path, fields: Mapping) -> None:
    """Merge fields into status.json atomically (0600)."""
    store.write_json_atomic(home / "status.json", read_status(home) | fields)


def freshness(status: Mapping) -> dict:
    """index_age_s and poller ok|stale (O8d); {} counts as stale."""
    return query._freshness(status or {})


def install_sha(env: Mapping[str, str]) -> str | None:
    """PCTX_INSTALL_SHA, else the pinned code dir that
    ~/.local/lib/provenance-context/current points at."""
    if env.get("PCTX_INSTALL_SHA"):
        return env["PCTX_INSTALL_SHA"]
    home = Path(env.get("HOME") or Path.home())
    current = home / ".local/lib/provenance-context/current"
    return Path(os.readlink(current)).name if current.is_symlink() else None


def run(argv):
    """launchctl and ps for doctor; tests replace it (never the real
    launchd domain)."""
    return subprocess.run(
        [str(a) for a in argv], capture_output=True, timeout=10
    )


def _hash_mismatches(home: Path) -> int:
    """open-time line hash failures, counted from the stage log."""
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
                continue
    return bad


# ponytail: untuned guesses; retune from real colleague databases.
DB_WARN_BYTES = 2 * 1024**3
FREE_WARN_RATIO = 0.25
FREE_WARN_BYTES = 64 * 1024**2


def human_bytes(n: float) -> str:
    """188.5 MB, 2.0 GB: 1024-based, units B KB MB GB TB."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def db_space(conn) -> dict:
    """page_count, freelist_count, page_size and the free-space ratio."""
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


def stats(conn, home: Path, env: Mapping[str, str], *, usage=False) -> dict:
    """Counts only (design 7): sources, events, flags, issues, knowledge,
    tombstones, the last pass; usage adds O9's per-session pctx calls."""

    def pairs(sql):
        return {r[0]: r[1] for r in conn.execute(sql)}

    flags = conn.execute(
        "SELECT total(flags & 1 > 0), total(flags & 2 > 0),"
        " total(flags & 4 > 0) FROM event"
    ).fetchone()
    status = read_status(home)
    db = store.db_path(home)
    out = {
        "sources": {
            "/".join(r[:4]): r[4]
            for r in conn.execute(
                "SELECT provider, root, thread_class, status, count(*)"
                " FROM source GROUP BY 1, 2, 3, 4"
            )
        },
        "events": pairs("SELECT kind, count(*) FROM event GROUP BY 1"),
        "events_by_provider": pairs(
            "SELECT s.provider, count(*) FROM event e"
            " JOIN source s ON s.id = e.source_id GROUP BY 1"
        ),
        "flags": dict(
            zip(("marker", "redacted", "truncated"), (int(n) for n in flags))
        ),
        "skipped_lines": conn.execute(
            "SELECT total(skipped_lines) FROM source"
        ).fetchone()[0],
        "issues": pairs("SELECT code, count(*) FROM source_issue GROUP BY 1"),
        "other_threads": pairs(  # format drift: never guessed primary
            "SELECT class_reason, count(*) FROM source"
            " WHERE thread_class = 'other' GROUP BY 1"
        ),
        "knowledge": pairs(
            "SELECT status, count(*) FROM knowledge GROUP BY 1"
        ),
        "citations": pairs("SELECT state, count(*) FROM citation GROUP BY 1"),
        "tombstones": pairs(
            "SELECT level, count(*) FROM tombstone GROUP BY 1"
        ),
        "db_bytes": db.stat().st_size if db.exists() else 0,
        "db_space": db_space(conn),
        "last_pass": {
            k: status.get(k)
            for k in (
                "last_pass_at",
                "duration_s",
                "files_changed",
                "events_added",
                "skipped_files",
                "failed",
                "errors",
                "busy_skips",
            )
        }
        | freshness(status),
        "install_sha": install_sha(env) or status.get("install_sha"),
        "classifier_version": classify.CLASSIFIER_VERSION,
        "hash_mismatches": _hash_mismatches(home),
    }
    out["skipped_lines"] = int(out["skipped_lines"])
    if usage:
        rows = conn.execute(
            "SELECT provider, session_root, sum(calls), sum(errors),"
            " max(last_ts) FROM usage GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        out["usage"] = [
            dict(
                zip(
                    ("provider", "session_root", "calls", "errors", "last_ts"),
                    r,
                )
            )
            for r in rows
        ]
        totals: dict[str, dict] = {}
        for provider, _, calls, errors, _ in rows:
            t = totals.setdefault(
                provider, {"calls": 0, "errors": 0, "sessions": 0}
            )
            t["calls"] += calls
            t["errors"] += errors
            t["sessions"] += 1
        out["usage_totals"] = totals
    return out


LABEL = "com.provenance-context"
DATA_FILES = frozenset(
    {
        "pctx.sqlite",
        "writer.lock",
        "status.json",
        "calls.jsonl",
        "calls.jsonl.1",
        "poller.log",
        "poller.log.1",
        "tombstones.jsonl",
        "recall.off",
    }
)
def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _job(env: Mapping[str, str]) -> dict | None:
    """The launchd job as {'pid', 'cmd'}; None when not loaded."""
    r = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    if r.returncode:
        return None
    found = re.search(rb"^\s*pid = (\d+)\s*$", r.stdout, re.M)
    if not found:
        return {"pid": None, "cmd": ""}
    ps = run(["ps", "-ww", "-o", "command=", "-p", found[1].decode()])
    return {
        "pid": int(found[1]),
        "cmd": ps.stdout.decode(errors="replace").strip(),
    }


def _writer_secure_delete() -> bool:
    """Writers turn secure_delete on (a per-connection setting, not stored
    in the file): apply the writer pragmas to an in-memory connection.
    Opening the real store would roll back a hot journal doctor reports."""
    conn = sqlite3.connect(":memory:")
    try:
        for pragma in store._WRITER_PRAGMAS:
            conn.execute(pragma)
        return conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()


def _lock_free(home: Path) -> bool:
    try:
        with store.writer_lock(home, wait_s=0):
            return True
    except (store.BusyError, OSError):
        return False




def doctor(home: Path, env: Mapping[str, str]) -> dict:
    """Health checks (design 7): errors decide ok; warn and info do not.
    Missing sources are information, never an error (D15)."""
    checks: list[dict] = []

    def check(name, ok, detail="", level="error"):
        ok = None if ok is None else bool(ok)
        checks.append(
            {"check": name, "ok": ok, "level": level, "detail": str(detail)}
        )

    present = home.is_dir()
    check(
        "data_dir_mode",
        present and _mode(home) == 0o700,
        oct(_mode(home)) if present else "absent",
    )
    names = sorted(p.name for p in home.iterdir()) if present else []
    loose = [
        n for n in names if (home / n).is_file() and _mode(home / n) & 0o077
    ]
    check("file_modes", not loose, ",".join(loose))
    stray = [
        n
        for n in names
        if n not in DATA_FILES
        and n != "pctx.sqlite-journal"
        and not n.startswith(".status.json.")
    ]
    check("unexpected_files", not stray, ",".join(stray))
    journal = (home / "pctx.sqlite-journal").exists()
    check(
        "unowned_journal",
        not (journal and _lock_free(home)),
        "a crashed writer's journal: the next writer rolls it back",
    )
    db = store.db_path(home)
    try:
        conn = store.connect_ro(db)
    except store.StoreUnavailableError as exc:
        check("store_readable", False, type(exc).__name__)
    else:
        try:
            check("store_readable", True)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            check("journal_mode", mode == "delete", mode)
            fts = [
                conn.execute(
                    f"SELECT v FROM {t}_config WHERE k = 'secure-delete'"
                ).fetchone()
                for t in ("event_fts", "knowledge_fts")
            ]
            check("fts_secure_delete", all(r and r[0] == 1 for r in fts))
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            check("quick_check", quick == "ok", quick[:80])
            missing = conn.execute(
                "SELECT count(*) FROM source WHERE status = 'missing'"
            ).fetchone()[0]
            check("missing_sources", True, missing, level="info")
            drift = conn.execute(
                "SELECT count(*) FROM source WHERE thread_class = 'other'"
            ).fetchone()[0]
            check("other_threads", True, drift, level="info")
            broken = conn.execute(
                "SELECT count(*) FROM citation c WHERE c.state = 'live'"
                " AND NOT EXISTS (SELECT 1 FROM event e JOIN source s"
                " ON s.id = e.source_id WHERE s.provider = c.provider AND"
                " s.thread_id = c.thread_id AND e.line = c.line AND"
                " e.part = c.part AND e.line_sha256 = c.line_sha256)"
            ).fetchone()[0]
            check("citations_resolve", not broken, broken, level="warn")
            space = db_space(conn)
            size = space["page_count"] * space["page_size"]
            check(
                "db_size",
                size < DB_WARN_BYTES,
                f"{human_bytes(size)}; threshold {human_bytes(DB_WARN_BYTES)}",
                level="warn",
            )
            free = space["freelist_count"] * space["page_size"]
            check(
                "db_free_space",
                not (
                    space["free_ratio"] > FREE_WARN_RATIO
                    and free > FREE_WARN_BYTES
                ),
                f"{human_bytes(free)} free ({space['free_ratio']:.0%});"
                " run: pctx compact",
                level="warn",
            )
        except sqlite3.Error as exc:
            check("store_readable", False, type(exc).__name__)
        finally:
            conn.close()
    check("writer_secure_delete", _writer_secure_delete())
    status = read_status(home)
    fresh = freshness(status)
    check("heartbeat", fresh["poller"] == "ok", fresh["index_age_s"])
    failed = status.get("failed") or 0
    check("failed_sources", not failed, failed, level="warn")
    job = _job(env)
    check("launchd_job", job is not None and job["pid"], job and job["pid"])
    roots = ingest.default_roots(env)
    blocked = [
        n
        for n, p in roots.items()
        if p.exists() and not os.access(p, os.R_OK | os.X_OK)
    ]
    check("roots_readable", not blocked, ",".join(blocked))
    absent = [n for n, p in roots.items() if not p.exists()]
    check("roots_present", True, ",".join(absent), level="info")
    ok = all(c["ok"] is not False for c in checks if c["level"] == "error")
    return {"ok": ok, "checks": checks}
