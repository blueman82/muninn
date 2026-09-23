#!/usr/bin/env python3
"""Build and query a local, source-cited session evidence index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from cli import doctor_is_healthy, parse_args
from cli import main as run_cli

__all__ = [
    "build_index",
    "doctor_is_healthy",
    "evidence_packet",
    "evidence_packet_from_connection",
    "main",
    "parse_args",
    "sanitize_text",
    "text_content",
]

SECRET_LINE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
    r"auth(?:orization)?|bearer|password|passwd|secret|private[_-]?key)"
    r"\s*(?:=|:)\s*\S+"
    r"|bearer\s+\S+|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
)
URL_CREDENTIAL = re.compile(
    r"(?i)(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
    r"://[^/\s@]+@\S+"
)
URL_SECRET_QUERY = re.compile(
    r"(?i)(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
    r"://\S+[?&](?:api[_-]?key|access[_-]?token|token|password|secret)="
    r"[^&\s]+"
)
QUOTED_SECRET_FIELD = re.compile(
    r'(?i)["\'](?:api[_-]?key|access[_-]?token|client[_-]?secret|token|'
    r'auth(?:orization)?|password|passwd|secret|private[_-]?key)["\']\s*:'
    r'\s*["\'][^"\']+["\']'
)
HOSTILE_LINE = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior)\s+instructions|"
    r"reveal\s+(?:the\s+)?(?:system|developer)\s+(?:prompt|message)|"
    r"follow\s+these\s+instructions\s+instead)"
)
SPLIT_SECRET = re.compile(
    r"(?is)(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
    r"password|passwd|secret)\s*\n\s*[:=]\s*\S+"
)
QUERY_TOKEN = re.compile(r"[A-Za-z0-9_]+")
RELAXED_QUERY_MIN_TOKENS = 3
RELAXED_QUERY_MAX_TOKENS = 4


def sanitize_text(text: str) -> str:
    """Suppress known credential and instruction patterns defensively."""
    if SPLIT_SECRET.search(text) or HOSTILE_LINE.search(text):
        return ""
    safe_lines = (
        line
        for line in text.splitlines()
        if not SECRET_LINE.search(line)
        and not URL_CREDENTIAL.search(line)
        and not URL_SECRET_QUERY.search(line)
        and not QUOTED_SECRET_FIELD.search(line)
        and not HOSTILE_LINE.search(line)
    )
    return "\n".join(safe_lines).strip()


def text_content(value: object) -> str:
    """Return textual message content from known JSON response shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "\n".join(filter(None, (text_content(item) for item in value)))
    return ""


def scalars(value: object) -> dict[str, str]:
    """Return trusted provenance fields from one direct record container."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: candidate
        for key in ("timestamp", "cwd", "repo", "repository")
        if isinstance(candidate := value.get(key), str) and candidate
    }


def messages(record: object) -> Iterator[Mapping[str, object]]:
    """Yield direct provider messages, never message-shaped tool output."""
    if not isinstance(record, Mapping):
        return
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return
    candidates = [payload]
    items = payload.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        candidates.extend(item for item in items if isinstance(item, Mapping))
    for value in candidates:
        if value.get("type") == "message" and value.get("role") in {
            "user",
            "assistant",
        }:
            yield value


def source_records(
    sessions_root: Path,
) -> Iterator[dict[str, str | int | None]]:
    """Read sanitized messages with JSONL provenance from session files."""
    for source_path in sorted(sessions_root.rglob("*.jsonl")):
        inherited: dict[str, str] = {}
        with source_path.open(encoding="utf-8") as source_file:
            for line_number, raw_line in enumerate(source_file, start=1):
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    message = f"invalid JSON at {source_path}:{line_number}"
                    raise ValueError(message) from error
                record_scalars = scalars(record)
                inherited.update(record_scalars)
                for message_ordinal, message in enumerate(
                    messages(record),
                    start=1,
                ):
                    text = sanitize_text(text_content(message.get("content")))
                    if not text:
                        continue
                    metadata = inherited | scalars(message)
                    yield {
                        "source_path": str(source_path),
                        "source_line": line_number,
                        "source_ordinal": message_ordinal,
                        "source_hash": hashlib.sha256(
                            raw_line.encode()
                        ).hexdigest(),
                        "timestamp": metadata.get("timestamp"),
                        "cwd": metadata.get("cwd"),
                        "repo": (
                            metadata.get("repo") or metadata.get("repository")
                        ),
                        "text": text,
                    }


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the disposable local index schema."""
    connection.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            source_ordinal INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            timestamp TEXT,
            cwd TEXT,
            repo TEXT,
            text TEXT NOT NULL,
            UNIQUE(source_path, source_line, source_hash, source_ordinal)
        );
        CREATE INDEX events_cwd ON events(cwd);
        CREATE VIRTUAL TABLE event_fts USING fts5(text);
        """
    )


def build_index(sessions_root: Path, db_path: Path) -> int:
    """Atomically rebuild an index from all session JSONL under a root."""
    if not sessions_root.is_dir():
        raise ValueError(f"sessions root is not a directory: {sessions_root}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=db_path.parent,
        prefix=f".{db_path.name}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            with connection:
                create_schema(connection)
                count = 0
                for event in source_records(sessions_root):
                    cursor = connection.execute(
                        """INSERT INTO events(
                        source_path, source_line, source_ordinal, source_hash,
                        timestamp,
                        cwd, repo, text
                    ) VALUES(
                        :source_path, :source_line, :source_ordinal,
                        :source_hash, :timestamp,
                        :cwd, :repo, :text
                    )""",
                        event,
                    )
                    connection.execute(
                        "INSERT INTO event_fts(rowid, text) VALUES(?, ?)",
                        (cursor.lastrowid, event["text"]),
                    )
                    count += 1
        os.replace(temporary_path, db_path)
        return count
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def fts_query(prompt: str) -> str:
    """Convert arbitrary prompt text into a conservative FTS5 AND query."""
    return " AND ".join(f'"{token}"' for token in QUERY_TOKEN.findall(prompt))


def relaxed_fts_query(prompt: str) -> str:
    """Require a two-token lexical match when exact recall has no result."""
    tokens = list(dict.fromkeys(QUERY_TOKEN.findall(prompt)))[
        :RELAXED_QUERY_MAX_TOKENS
    ]
    if len(tokens) < RELAXED_QUERY_MIN_TOKENS:
        return ""
    return " OR ".join(
        f'("{left}" AND "{right}")'
        for index, left in enumerate(tokens)
        for right in tokens[index + 1 :]
    )


def fetch_matches(
    connection: sqlite3.Connection,
    query: str,
    repo: str | None,
    include_provider: bool = False,
) -> list[sqlite3.Row]:
    """Fetch one prebuilt lexical query, enforcing repository scope in SQL."""
    if not query:
        return []
    scope = (
        "AND (events.cwd = :repo "
        "OR (events.cwd IS NULL AND events.repo = :repo))"
        if repo
        else ""
    )
    provider_column = (
        "events.provider AS provider," if include_provider else ""
    )
    source_filter = ""
    try:
        has_sources = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sources'"
        ).fetchone()
    except sqlite3.Error:
        has_sources = None
    if has_sources:
        source_filter = (
            " JOIN sources ON sources.provider = events.provider "
            "AND sources.source_path = events.source_path "
        )
    active_filter = (
        "AND sources.status = 'active' AND sources.pending = 0"
        if has_sources
        else ""
    )
    return connection.execute(
        f"""SELECT events.source_path, events.source_line,
                   events.source_ordinal, events.source_hash,
                   {provider_column}
                   events.timestamp,
                   events.cwd, events.repo, events.text
            FROM event_fts JOIN events ON events.id = event_fts.rowid
            {source_filter} WHERE event_fts MATCH :query {scope}
            {active_filter}
            ORDER BY bm25(event_fts), events.source_path, events.source_line
            LIMIT 50""",
        {"query": query, "repo": repo},
    ).fetchall()


def evidence_packet_from_connection(
    connection: sqlite3.Connection,
    prompt: str,
    repo: str | None,
    max_bytes: int,
    include_provider: bool = False,
) -> dict[str, object]:
    """Return bounded, cited evidence from one open SQLite snapshot."""
    if max_bytes < 1:
        raise ValueError("max bytes must be positive")
    connection.row_factory = sqlite3.Row
    exact_query = fts_query(prompt)
    matches = fetch_matches(connection, exact_query, repo, include_provider)
    global_fallback = bool(repo and not matches)
    if global_fallback:
        matches = fetch_matches(
            connection, exact_query, None, include_provider
        )
    relaxed = False
    if global_fallback and not matches:
        matches = fetch_matches(
            connection, relaxed_fts_query(prompt), None, include_provider
        )
        relaxed = bool(matches)
    evidence: list[dict[str, object]] = []
    used_bytes = 0
    for match in matches:
        item = {
            "text": f"[Untrusted historical evidence]\n{match['text']}",
            "source": {
                "path": match["source_path"],
                "line": match["source_line"],
                "ordinal": match["source_ordinal"],
                "hash": match["source_hash"],
            },
            "timestamp": match["timestamp"],
            "cwd": match["cwd"],
            "repo": match["repo"],
        }
        if include_provider:
            item["source"]["provider"] = match["provider"]
        item_size = len(json.dumps(item, ensure_ascii=False).encode())
        if item_size > max_bytes - used_bytes:
            continue
        evidence.append(item)
        used_bytes += item_size
    packet: dict[str, object] = {
        "evidence": evidence,
        "bytes": used_bytes,
        "untrusted": True,
    }
    if global_fallback and evidence:
        packet["retrieval_scope"] = "global_historical_fallback"
        if relaxed:
            packet["match_strategy"] = "lexical_relaxed"
    return packet


def evidence_packet(
    db_path: Path,
    prompt: str,
    repo: str | None,
    max_bytes: int,
) -> dict[str, object]:
    """Return bounded, source-cited untrusted historical evidence."""
    with closing(sqlite3.connect(db_path)) as connection:
        return evidence_packet_from_connection(
            connection, prompt, repo, max_bytes
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the builder or evidence retrieval command."""
    return run_cli(build_index, evidence_packet, argv)


if __name__ == "__main__":
    raise SystemExit(main())
