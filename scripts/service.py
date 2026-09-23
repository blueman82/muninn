"""Run and query the local, single-writer provenance context service."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingMixIn, UnixStreamServer
from typing import Optional, cast

from context import evidence_packet
from normalizers import discover, parse_source

MAX_PACKET_BYTES = 2_400


def _json(value: object) -> bytes:
    """Encode one bounded protocol payload."""
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


def _load_json(path: Path) -> dict[str, object]:
    """Read an object JSON file, returning an empty object when absent."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    """Atomically write an operational JSON artifact with private mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(json.dumps(value, sort_keys=True))
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def _audit(
    state_dir: Path, event: str, trace_id: str, **fields: object
) -> None:
    """Append redacted lifecycle data without prompt or source text."""
    record = {"at": time.time(), "event": event, "trace_id": trace_id} | fields
    audit_path = state_dir / "audit.jsonl"
    with audit_path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(record, sort_keys=True) + "\n")
    os.chmod(audit_path, 0o600)


def _create_schema(connection: sqlite3.Connection) -> None:
    """Create the disposable snapshot schema."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(events)").fetchall()
    }
    if columns and "provider" not in columns:
        # The old archive snapshot is disposable and lacks source identities.
        connection.executescript(
            "DROP TABLE IF EXISTS assertions; DROP TABLE IF EXISTS sources;"
            "DROP TABLE IF EXISTS event_fts; DROP TABLE IF EXISTS events;"
        )
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            source_ordinal INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            timestamp TEXT,
            cwd TEXT,
            repo TEXT,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(provider, source_path, source_line, source_hash,
                   source_ordinal)
        );
        CREATE TABLE IF NOT EXISTS sources (
            provider TEXT NOT NULL,
            source_path TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            last_seen REAL NOT NULL,
            PRIMARY KEY(provider, source_path)
        );
        CREATE TABLE IF NOT EXISTS assertions (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id),
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            state TEXT NOT NULL,
            supersedes INTEGER REFERENCES assertions(id),
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_cwd ON events(cwd);
        CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(text);
        """
    )


def _open_snapshot(destination: Path) -> tuple[Path, sqlite3.Connection]:
    """Create a private replacement database, copying the prior snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    if destination.exists():
        shutil.copy2(destination, temporary_path)
    connection = sqlite3.connect(temporary_path)
    _create_schema(connection)
    return temporary_path, connection


def _source_rows(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, int]]:
    """Return the last known source fingerprints and retry flags."""
    return {
        (provider, path): (fingerprint, pending)
        for provider, path, fingerprint, pending in connection.execute(
            "SELECT provider, source_path, fingerprint, pending FROM sources"
        )
    }


def _insert_event(
    connection: sqlite3.Connection, event: Mapping[str, object]
) -> int:
    """Insert an event and its lexical index row."""
    cursor = connection.execute(
        """INSERT INTO events(provider, source_path, source_line,
           source_ordinal,
           source_hash, timestamp, cwd, repo, role, text)
           VALUES(:provider, :source_path, :source_line, :source_ordinal,
                  :source_hash, :timestamp, :cwd, :repo, :role, :text)""",
        event,
    )
    row_id = cursor.lastrowid
    if row_id is None:
        raise sqlite3.Error("event insert did not return a row id")
    connection.execute(
        "INSERT INTO event_fts(rowid, text) VALUES(?, ?)",
        (row_id, event["text"]),
    )
    return int(row_id)


def _preference(text: str) -> Optional[str]:
    """Return a direct first-person preference, never an inferred fact."""
    lowered = text.strip().lower()
    for prefix in ("i prefer ", "please always ", "please never "):
        if lowered.startswith(prefix) and len(text) > len(prefix):
            return text.strip()
    return None


def _replace_source(
    connection: sqlite3.Connection,
    provider: str,
    source_path: str,
    fingerprint: str,
    events: Sequence[Mapping[str, object]],
    pending: bool,
    error: str | None,
) -> int:
    """Replace one source atomically after its complete prefix was parsed."""
    row_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
    ]
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        connection.execute(
            f"DELETE FROM assertions WHERE event_id IN ({placeholders})",
            row_ids,
        )
        connection.execute(
            f"DELETE FROM event_fts WHERE rowid IN ({placeholders})", row_ids
        )
    connection.execute(
        "DELETE FROM events WHERE provider = ? AND source_path = ?",
        (provider, source_path),
    )
    count = 0
    for event in events:
        event_id = _insert_event(connection, event)
        if event["role"] == "user" and (
            value := _preference(str(event["text"]))
        ):
            previous = connection.execute(
                """SELECT id FROM assertions WHERE kind = 'preference'
                   AND state = 'active' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if previous:
                connection.execute(
                    "UPDATE assertions SET state = 'superseded' WHERE id = ?",
                    (previous[0],),
                )
            connection.execute(
                """INSERT INTO assertions(event_id, kind, value, state,
                   supersedes,
                   created_at) VALUES(?, 'preference', ?, 'active', ?, ?)""",
                (
                    event_id,
                    value,
                    previous[0] if previous else None,
                    time.time(),
                ),
            )
        count += 1
    connection.execute(
        """INSERT INTO sources(provider, source_path, fingerprint, pending,
           error,
           last_seen) VALUES(?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, source_path) DO UPDATE SET
             fingerprint=excluded.fingerprint, pending=excluded.pending,
             error=excluded.error, last_seen=excluded.last_seen""",
        (provider, source_path, fingerprint, int(pending), error, time.time()),
    )
    return count


def _delete_missing(
    connection: sqlite3.Connection,
    current: set[tuple[str, str]],
) -> int:
    """Remove sources that disappeared or were renamed from the snapshot."""
    removed = 0
    for provider, source_path in _source_rows(connection):
        if (provider, source_path) in current:
            continue
        row_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM events WHERE provider = ? AND source_path = ?",
                (provider, source_path),
            )
        ]
        if row_ids:
            placeholders = ",".join("?" for _ in row_ids)
            connection.execute(
                f"DELETE FROM assertions WHERE event_id IN ({placeholders})",
                row_ids,
            )
            connection.execute(
                f"DELETE FROM event_fts WHERE rowid IN ({placeholders})",
                row_ids,
            )
        connection.execute(
            "DELETE FROM events WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
        connection.execute(
            "DELETE FROM sources WHERE provider = ? AND source_path = ?",
            (provider, source_path),
        )
        removed += 1
    return removed


class Brain:
    """Own the service snapshot, reconciliation loop, and redacted state."""

    def __init__(
        self,
        codex_root: Path,
        claude_root: Path,
        database: Path,
        state_dir: Path,
    ) -> None:
        self.codex_root = codex_root
        self.claude_root = claude_root
        self.database = database
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.generation = 0
        self.last_publish = 0.0
        self.last_scan = 0.0
        self.last_error = ""
        self.pending = 0
        self.snapshot_hash = ""
        self.lock = threading.Lock()

    def reconcile(self) -> None:
        """Publish a new snapshot when source fingerprints require it."""
        trace_id = uuid.uuid4().hex
        started = time.monotonic()
        with self.lock:
            temporary, connection = _open_snapshot(self.database)
            try:
                known = _source_rows(connection)
                current: set[tuple[str, str]] = set()
                changed = False
                indexed = 0
                pending = 0
                errors: list[str] = []
                for provider, root in (
                    ("codex", self.codex_root),
                    ("claude", self.claude_root),
                ):
                    for source_path, path, fingerprint in discover(root):
                        key = (provider, source_path)
                        current.add(key)
                        previous = known.get(key)
                        if previous == (fingerprint, 0):
                            continue
                        events, error, source_pending = parse_source(
                            provider, root, path
                        )
                        if error:
                            errors.append(error)
                            pending += 1
                            continue
                        indexed += _replace_source(
                            connection,
                            provider,
                            source_path,
                            fingerprint,
                            events,
                            source_pending,
                            None,
                        )
                        changed = True
                        pending += int(source_pending)
                if _delete_missing(connection, current):
                    changed = True
                self.last_scan = time.time()
                self.pending = pending
                self.last_error = errors[0] if errors else ""
                if changed or not self.database.exists():
                    connection.commit()
                    connection.close()
                    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.database)
                    self.generation += 1
                    self.last_publish = time.time()
                    self.snapshot_hash = digest
                    _audit(
                        self.state_dir,
                        "publish",
                        trace_id,
                        generation=self.generation,
                        indexed=indexed,
                        pending=pending,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        snapshot_hash=digest,
                    )
                else:
                    connection.close()
                    temporary.unlink(missing_ok=True)
                self._write_state()
            except (OSError, sqlite3.Error, ValueError) as error:
                connection.close()
                temporary.unlink(missing_ok=True)
                self.last_error = type(error).__name__
                self.last_scan = time.time()
                self._write_state()
                _audit(
                    self.state_dir,
                    "reconcile_error",
                    trace_id,
                    error=self.last_error,
                )

    def _write_state(self) -> None:
        """Write low-cardinality health state without raw source details."""
        sources: list[dict[str, object]] = []
        if self.database.exists():
            try:
                with sqlite3.connect(self.database) as connection:
                    for (
                        provider,
                        path,
                        pending,
                        error,
                        seen,
                    ) in connection.execute(
                        "SELECT provider, source_path, pending, error, "
                        "last_seen FROM sources"
                    ):
                        sources.append(
                            {
                                "provider": provider,
                                "source_id": hashlib.sha256(
                                    path.encode()
                                ).hexdigest()[:16],
                                "pending": bool(pending),
                                "error": error,
                                "staleness_seconds": round(
                                    max(0, time.time() - seen), 3
                                ),
                            }
                        )
            except sqlite3.Error:
                self.last_error = "legacy_snapshot"
        _write_json(
            self.state_dir / "state.json",
            {
                "generation": self.generation,
                "last_scan": self.last_scan,
                "last_publish": self.last_publish,
                "snapshot_hash": self.snapshot_hash,
                "pending_sources": self.pending,
                "last_error": self.last_error,
                "sources": sources,
                "otel": "trace-compatible IDs only; exporter requires "
                "Python >=3.10",
            },
        )

    def recall(self, request: Mapping[str, object]) -> dict[str, object]:
        """Return bounded cited evidence from the published snapshot."""
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not self.database.exists():
            return {"evidence": [], "bytes": 0, "untrusted": True}
        maximum = request.get("max_bytes", MAX_PACKET_BYTES)
        maximum = maximum if isinstance(maximum, int) else MAX_PACKET_BYTES
        repo = request.get("repo")
        packet = evidence_packet(
            self.database,
            prompt,
            repo if isinstance(repo, str) else None,
            min(max(maximum, 1), MAX_PACKET_BYTES),
        )
        evidence = packet.get("evidence")
        if not isinstance(evidence, list):
            return packet
        for item in evidence:
            if isinstance(item, dict) and isinstance(item.get("source"), dict):
                source = cast(dict[str, object], item["source"])
                source["provider"] = self._provider_for(source)
        return packet

    def _provider_for(self, source: Mapping[str, object]) -> str:
        """Find an event provider from its source reference."""
        if not self.database.exists():
            return "unknown"
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                """SELECT provider FROM events WHERE source_path = ?
                   AND source_line = ?
                   AND source_ordinal = ? AND source_hash = ? LIMIT 1""",
                (
                    source.get("path"),
                    source.get("line"),
                    source.get("ordinal"),
                    source.get("hash"),
                ),
            ).fetchone()
        return str(row[0]) if row else "unknown"

    def status(self) -> dict[str, object]:
        """Return public redacted health state."""
        return _load_json(self.state_dir / "state.json") | {"available": True}


class RequestHandler(StreamRequestHandler):
    """Serve a one-line JSON request through a Unix-domain socket."""

    brain: Brain

    def handle(self) -> None:
        """Decode, service, and return a single bounded request."""
        started = time.monotonic()
        trace_id = uuid.uuid4().hex
        try:
            raw = self.rfile.readline(8192)
            request = json.loads(raw)
            if not isinstance(request, Mapping):
                raise ValueError("request")
            operation = request.get("op")
            if operation == "recall":
                response = self.brain.recall(request)
            elif operation in {"status", "doctor"}:
                response = self.brain.status()
            else:
                response = {"error": "unsupported_operation"}
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
            response = {"error": "invalid_request"}
        response["trace_id"] = trace_id
        self.wfile.write(_json(response))
        _audit(
            self.brain.state_dir,
            "client",
            trace_id,
            status="ok" if "error" not in response else "error",
            latency_ms=round((time.monotonic() - started) * 1000),
        )


class Server(ThreadingMixIn, UnixStreamServer):
    """Threaded readers around one Brain reconciliation writer."""

    daemon_threads = True


def request(
    socket_path: Path, payload: Mapping[str, object]
) -> dict[str, object]:
    """Call the local service with a bounded unavailable-safe fallback."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(socket_path))
            client.sendall(_json(payload))
            response = json.loads(client.makefile("rb").readline(8192))
        return dict(response) if isinstance(response, Mapping) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        if payload.get("op") == "recall":
            return {
                "evidence": [],
                "bytes": 0,
                "untrusted": True,
                "available": False,
            }
        return {"available": False, "status": "unavailable"}


def serve(
    codex_root: Path,
    claude_root: Path,
    database: Path,
    state_dir: Path,
    socket_path: Path,
    interval: float,
) -> int:
    """Run the reconciler and Unix socket service until signalled to stop."""
    state_dir.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    brain = Brain(codex_root, claude_root, database, state_dir)
    brain.reconcile()
    server = Server(str(socket_path), RequestHandler)
    RequestHandler.brain = brain
    os.chmod(socket_path, 0o600)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    next_scan = time.monotonic() + interval
    try:
        while not stop.is_set():
            server.timeout = min(0.2, max(0.01, next_scan - time.monotonic()))
            server.handle_request()
            if time.monotonic() >= next_scan:
                brain.reconcile()
                next_scan = time.monotonic() + interval
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)
    return 0
