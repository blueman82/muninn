"""pctx erase (design 4.3, 3.8, 5; spec O4d, O10, O13, A13).

erase() needs the caller to hold store.writer_lock; run_erase() takes it.
Targets are collected first; one transaction then inserts tombstones,
deletes events (FTS rows under secure-delete), scrubs knowledge text and
quotes and deletes the source rows of erased sessions.  Tombstones and
logs hold ids and hashes only; residue needles and rare terms stay in
memory.  Provider transcripts are never touched, only listed.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pctx import ingest, query, store

MIN_MATCH = 4  # a 1-3 character match would erase nearly everything
TOMBSTONE_FILE = "tombstones.jsonl"
NEEDLES, NEEDLE_BYTES = 50, 48
NOT_COVERED = (
    "provider transcripts: pctx never deletes them (see provider_files)",
    "Time Machine and other backups",
    "free filesystem blocks (FileVault encrypts them at rest)",
)
_DERIVED = (
    ".codex/plugins/cache/provenance-context-local",
    ".codex/memories",
)


@dataclass
class _Target:
    sessions: set[tuple[str, str]] = field(default_factory=set)
    sources: dict[int, sqlite3.Row] = field(default_factory=dict)  # deleted
    touched: dict[int, sqlite3.Row] = field(default_factory=dict)  # listed
    lines: set[tuple[int, int]] = field(default_factory=set)
    events: list[int] = field(default_factory=list)
    tombstones: list[dict] = field(default_factory=list)
    citations: set[int] = field(default_factory=set)
    knowledge: set[int] = field(default_factory=set)


def erase(
    conn: sqlite3.Connection,
    *,
    home: Path,
    session: str | None = None,
    event_ref: str | None = None,
    match: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] = os.environ,
) -> dict:
    """Erase one session (with its forks), one event line, or every text
    containing match.  The caller holds store.writer_lock.  Returns counts
    and paths only: never erased text, never the match string."""
    picked = [
        mode
        for mode, value in (
            ("session", session),
            ("event", event_ref),
            ("match", match),
        )
        if value is not None
    ]
    if len(picked) != 1:
        raise ValueError("give exactly one of session, event_ref, match")
    if match is not None and len(match.strip()) < MIN_MATCH:
        raise ValueError(f"match needs at least {MIN_MATCH} characters")
    t = _Target()
    if session is not None:
        _collect_session(conn, t, session)
    elif event_ref is not None:
        _collect_event(conn, t, event_ref)
    else:
        _collect_match(conn, t, match)
    _collect_knowledge(conn, t, match)
    out = {
        "dry_run": dry_run,
        "mode": picked[0],
        "sessions": len(t.sessions),
        "sources": len(t.sources),
        "lines": len(t.lines),
        "events": len(t.events),
        "citations": len(t.citations),
        "knowledge": len(t.knowledge),
        "tombstones": len(t.tombstones),
        "provider_files": _provider_files(t, ingest.default_roots(env)),
        "out_of_scope": _out_of_scope(env),
        "not_covered": list(NOT_COVERED),
    }
    if dry_run:
        return out
    needles = [match.encode()] if match else _needles(conn, t)
    rare = _rare_terms(conn, t.events, sorted(t.knowledge))
    _append_tombstones(home, t.tombstones)  # write-ahead, F_FULLFSYNC
    try:
        conn.execute("BEGIN IMMEDIATE")
        _apply(conn, t)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if not match:  # a needle still in a surviving row is not residue
        needles = [n for n in needles if not _still_stored(conn, n)]
    out["vocab"] = _vocab_left(conn, rare)
    out["needles"] = len(needles)
    out["residue"] = residue_scan(home, needles) or 0
    return out


def run_erase(home: Path, *, wait_s: float = 15.0, **kw) -> dict:
    """Take the writer lock, open the store (fullfsync on, O4d), erase."""
    with store.writer_lock(home, wait_s=wait_s):
        conn = store.connect_rw(store.db_path(home))
        try:
            return erase(conn, home=home, **kw)
        finally:
            conn.close()


def _collect_session(conn, t: _Target, session: str) -> None:
    """The session, every session forked from one of its threads, and so
    on (a fork's copied prefix is the erased session's content, 3.8)."""
    providers = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT provider FROM source WHERE session_root = ?",
            (session,),
        )
    ] or [
        "codex",
        "claude",
    ]  # not ingested yet: block it for later
    frontier = {(provider, session) for provider in providers}
    while frontier:
        t.sessions |= frontier
        found = set()
        for provider, root in frontier:
            rows = conn.execute(
                "SELECT * FROM source WHERE provider = ? AND session_root = ?",
                (provider, root),
            ).fetchall()
            threads = {root} | {r["thread_id"] for r in rows}
            t.sources.update((r["id"], r) for r in rows)
            marks = ",".join("?" * len(threads))
            forks = conn.execute(
                "SELECT DISTINCT provider, session_root FROM source"
                f" WHERE provider = ? AND forked_from_id IN ({marks})",
                (provider, *threads),
            )
            found |= {tuple(f) for f in forks} - t.sessions
        frontier = found
    t.touched.update(t.sources)
    t.tombstones = [
        _tombstone(provider, "session", session_root=root)
        for provider, root in sorted(t.sessions)
    ]
    for source_id in t.sources:
        t.events += [
            r[0]
            for r in conn.execute(
                "SELECT id FROM event WHERE source_id = ?", (source_id,)
            )
        ]


def _collect_event(conn, t: _Target, ref: str) -> None:
    provider, thread, line, part = query.parse_ref(ref)  # WU5 format
    sources = conn.execute(
        "SELECT * FROM source WHERE provider = ? AND substr(thread_id, 1, ?)"
        " = ?",
        (provider, len(thread), thread),
    ).fetchall()
    if len(sources) != 1:
        raise LookupError(f"{len(sources)} threads match the ref")
    parts = {
        r[0]
        for r in conn.execute(
            "SELECT part FROM event WHERE source_id = ? AND line = ?",
            (sources[0]["id"], line),
        )
    }
    if part not in parts:
        raise LookupError("no such event")
    _add_line(conn, t, sources[0], line)


def _collect_match(conn, t: _Target, match: str) -> None:
    hits = conn.execute(
        "SELECT DISTINCT source_id, line FROM event WHERE instr(text, ?) > 0",
        (match,),
    ).fetchall()
    for source_id, line in hits:
        source = conn.execute(
            "SELECT * FROM source WHERE id = ?", (source_id,)
        ).fetchone()
        _add_line(conn, t, source, line)
    t.knowledge |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM knowledge WHERE text IS NOT NULL"
            " AND instr(text, ?) > 0",
            (match,),
        )
    }
    t.citations |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM citation WHERE quote IS NOT NULL"
            " AND instr(quote, ?) > 0",
            (match,),
        )
    }


def _add_line(conn, t: _Target, source, line: int) -> None:
    """Every part of one line: a line tombstone blocks the whole line."""
    rows = conn.execute(
        "SELECT id, line_sha256 FROM event WHERE source_id = ? AND line = ?",
        (source["id"], line),
    ).fetchall()
    t.lines.add((source["id"], line))
    t.touched[source["id"]] = source
    t.events += [r[0] for r in rows]
    t.tombstones.append(
        _tombstone(
            source["provider"],
            "line",
            thread_id=source["thread_id"],
            line=line,
            line_sha256=rows[0][1],
        )
    )


def _collect_knowledge(conn, t: _Target, match: str | None) -> None:
    """Citations of erased events are erased; an entry is erased when its
    text matched or when every one of its citations is erased."""
    for source in t.sources.values():
        t.citations |= _cites(
            conn,
            "provider = ? AND thread_id = ?",
            (source["provider"], source["thread_id"]),
        )
    for source_id, line in t.lines:
        source = t.touched[source_id]
        t.citations |= _cites(
            conn,
            "provider = ? AND thread_id = ? AND line = ?",
            (source["provider"], source["thread_id"], line),
        )
    if not t.citations:
        return
    marks = ",".join("?" * len(t.citations))
    entries = conn.execute(
        "SELECT knowledge_id, sum(id NOT IN"
        f" ({marks})) FROM citation WHERE state = 'live'"
        " GROUP BY knowledge_id",
        tuple(t.citations),
    ).fetchall()
    uncited = {kid for kid, live_left in entries if live_left == 0}
    t.knowledge |= {
        r[0]
        for r in conn.execute(
            "SELECT id FROM knowledge WHERE status != 'erased'"
            f" AND id IN ({','.join('?' * len(uncited)) or 'NULL'})",
            tuple(uncited),
        )
    }


def _cites(conn, where: str, args: tuple) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM citation WHERE state = 'live' AND {where}", args
        )
    }


def _tombstone(provider: str, level: str, **ids) -> dict:
    """Ids and hashes only (design 3.8): never text, never the match."""
    row = dict.fromkeys(("session_root", "thread_id", "line", "line_sha256"))
    row.update(ids, provider=provider, level=level, created_at=time.time())
    return row


def _apply(conn, t: _Target) -> None:
    """The writes, inside one transaction (design 4.3 #3)."""
    conn.executemany(
        "INSERT INTO tombstone(created_at, provider, level, session_root,"
        " thread_id, line, line_sha256) VALUES (:created_at, :provider,"
        " :level, :session_root, :thread_id, :line, :line_sha256)",
        t.tombstones,
    )
    conn.executemany(  # the event_ad trigger removes the FTS rows
        "DELETE FROM event WHERE id = ?", [(i,) for i in t.events]
    )
    conn.executemany(
        "UPDATE citation SET quote = NULL, span_start = NULL,"
        " span_end = NULL, state = 'erased' WHERE id = ?",
        [(i,) for i in sorted(t.citations)],
    )
    now = time.time()
    for kid in sorted(t.knowledge):
        conn.execute(
            "UPDATE knowledge SET text = NULL, retract_reason = NULL,"
            " status = 'erased' WHERE id = ?",
            (kid,),
        )
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (?, 'erase', 'user', ?)",
            (kid, now),
        )
    conn.executemany(  # source_issue and usage rows cascade
        "DELETE FROM source WHERE id = ?", [(i,) for i in t.sources]
    )


def _texts(conn, t: _Target) -> list[str]:
    """The texts about to be erased (in memory only)."""
    texts = []
    for table, column, ids in (
        ("event", "text", t.events),
        ("knowledge", "text", sorted(t.knowledge)),
        ("citation", "quote", sorted(t.citations)),
    ):
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            texts += [
                r[0]
                for r in conn.execute(
                    f"SELECT {column} FROM {table} WHERE id IN"
                    f" ({','.join('?' * len(chunk))}) AND {column} NOT NULL",
                    chunk,
                )
            ]
    return texts


def _needles(conn, t: _Target) -> list[bytes]:
    """First 48 bytes of up to 50 erased texts, the longest first."""
    found: list[bytes] = []
    for text in sorted(_texts(conn, t), key=len, reverse=True):
        head = text.encode("utf-8", "surrogatepass")[:NEEDLE_BYTES]
        if len(head) >= 12 and head not in found:
            found.append(head)
        if len(found) == NEEDLES:
            break
    return found


def _still_stored(conn, needle: bytes) -> bool:
    """A surviving row holds the same bytes (e.g. a repeated prompt)."""
    for table, column in (
        ("event", "text"),
        ("knowledge", "text"),
        ("citation", "quote"),
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE instr(CAST({column} AS BLOB), ?)"
            " LIMIT 1",
            (needle,),
        ).fetchone():
            return True
    return False


def _vocab(conn, table: str, kind: str) -> str:
    name = f"pctx_{kind}_{table}"
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{name}"
        f" USING fts5vocab(main, {table}, {kind})"
    )
    return f"temp.{name}"


def _rare_terms(conn, event_ids, knowledge_ids) -> dict[str, set[str]]:
    """O10: per FTS table, the terms found only in the erased documents
    and in at most 3 of them (in memory, never stored)."""
    rare: dict[str, set[str]] = {}
    for table, ids in (
        ("event_fts", event_ids),
        ("knowledge_fts", knowledge_ids),
    ):
        rare[table] = set()
        if not ids:
            continue
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS pctx_erase_doc"
            " (id INTEGER PRIMARY KEY)"
        )
        conn.execute("DELETE FROM temp.pctx_erase_doc")
        conn.executemany(
            "INSERT OR IGNORE INTO temp.pctx_erase_doc(id) VALUES (?)",
            [(i,) for i in ids],
        )
        instance, row = _vocab(conn, table, "instance"), _vocab(
            conn, table, "row"
        )
        counts = conn.execute(
            f"SELECT v.term, count(DISTINCT v.doc) FROM {instance} v"
            " JOIN temp.pctx_erase_doc d ON d.id = v.doc GROUP BY v.term"
        ).fetchall()
        for term, erased in counts:
            if erased > 3:
                continue
            df = conn.execute(
                f"SELECT doc FROM {row} WHERE term = ?", (term,)
            ).fetchone()
            if df is not None and df[0] == erased:
                rare[table].add(term)
    return rare


def _vocab_left(conn, rare: dict[str, set[str]]) -> int:
    """How many of those terms either FTS index still knows (0 = clean)."""
    left = 0
    for table, terms in rare.items():
        if terms:
            row = _vocab(conn, table, "row")
            left += sum(
                conn.execute(
                    f"SELECT 1 FROM {row} WHERE term = ?", (term,)
                ).fetchone()
                is not None
                for term in terms
            )
    return left


def residue_scan(home: Path, needles: list[bytes]) -> list[str]:
    """Files under home (journal included) holding any needle, relative."""
    if not needles:
        return []
    hits = []
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                hits.append(path.relative_to(home).as_posix())
    return hits


_KEY = (
    "provider",
    "level",
    "session_root",
    "thread_id",
    "line",
    "line_sha256",
)


def _append_tombstones(home: Path, tombstones: list[dict]) -> None:
    """O4d: append (ids and hashes only) to tombstones.jsonl, 0600, and
    F_FULLFSYNC before the DB commit, so a rebuild can re-apply them."""
    if not tombstones:
        return
    payload = b"".join(
        json.dumps(row, sort_keys=True).encode() + b"\n" for row in tombstones
    )
    fd = os.open(
        home / TOMBSTONE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except (AttributeError, OSError):  # not macOS, or not supported
            os.fsync(fd)
    finally:
        os.close(fd)


def _valid(row: dict) -> bool:
    level = row.get("level")
    return row.get("provider") in ("codex", "claude") and (
        (level == "session" and bool(row.get("session_root")))
        or (level == "thread" and bool(row.get("thread_id")))
        or (
            level == "line"
            and bool(row.get("thread_id"))
            and isinstance(row.get("line"), int)
            and bool(row.get("line_sha256"))
        )
    )


def reapply_tombstones(conn: sqlite3.Connection, home: Path) -> int:
    """Insert the tombstone rows from tombstones.jsonl that the table lacks
    (e.g. after a rebuild); returns how many.  The caller holds the writer
    lock.  Invalid lines are skipped."""
    path = home / TOMBSTONE_FILE
    if not path.is_file():
        return 0
    have = {
        tuple(r)
        for r in conn.execute(f"SELECT {', '.join(_KEY)} FROM tombstone")
    }
    missing = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or not _valid(row):
            continue
        key = tuple(row.get(k) for k in _KEY)
        if key not in have:
            have.add(key)
            missing.append((row.get("created_at") or time.time(), *key))
    if missing:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            f"INSERT INTO tombstone(created_at, {', '.join(_KEY)})"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            missing,
        )
        conn.execute("COMMIT")
    return len(missing)


def _provider_files(t: _Target, roots: dict[str, Path]) -> list[str]:
    """Transcript files the erased content came from (never touched)."""
    return sorted(
        {
            str(roots[s["root"]] / s["path"])
            for s in t.touched.values()
            if s["root"] in roots
        }
    )


def _out_of_scope(env: Mapping[str, str]) -> list[str]:
    """Other derived copies that exist: listed for the owner, never
    deleted (O13, A13)."""
    home = Path(env.get("HOME") or Path.home())
    found = [str(home / rel) for rel in _DERIVED if (home / rel).exists()]
    return sorted(found)
