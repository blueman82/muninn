#!/usr/bin/env python3
"""Build and query a local, source-cited session evidence index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

SECRET_LINE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|token|"
    r"auth(?:orization)?|bearer|password|passwd|secret|private[_-]?key)"
    r"\s*(?:=|:)\s*\S+"
    r"|bearer\s+\S+|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
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


def sanitize_text(text: str) -> str:
    """Suppress known credential and instruction patterns defensively."""
    if SPLIT_SECRET.search(text) or HOSTILE_LINE.search(text):
        return ""
    safe_lines = (
        line
        for line in text.splitlines()
        if not SECRET_LINE.search(line)
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
    """Find the first supported provenance fields in a JSON value."""
    found: dict[str, str] = {}

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key in ("timestamp", "cwd", "repo", "repository"):
                candidate = item.get(key)
                if (
                    key not in found
                    and isinstance(candidate, str)
                    and candidate
                ):
                    found[key] = candidate
            for child in item.values():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for child in item:
                visit(child)

    visit(value)
    return found


def messages(value: object) -> Iterator[Mapping[str, object]]:
    """Yield user and assistant message objects from nested JSON records."""
    if isinstance(value, Mapping):
        if value.get("type") == "message" and value.get("role") in {
            "user",
            "assistant",
        }:
            yield value
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from messages(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from messages(child)


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
        with sqlite3.connect(temporary_path) as connection:
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


def fetch_matches(
    connection: sqlite3.Connection,
    prompt: str,
    repo: str | None,
) -> list[sqlite3.Row]:
    """Fetch lexical matches, enforcing repository scope in SQL."""
    query = fts_query(prompt)
    if not query:
        return []
    scope = (
        "AND (events.cwd = :repo "
        "OR (events.cwd IS NULL AND events.repo = :repo))"
        if repo
        else ""
    )
    return connection.execute(
        f"""SELECT events.source_path, events.source_line,
                   events.source_ordinal, events.source_hash,
                   events.timestamp,
                   events.cwd, events.repo, events.text
            FROM event_fts JOIN events ON events.id = event_fts.rowid
            WHERE event_fts MATCH :query {scope}
            ORDER BY bm25(event_fts), events.source_path, events.source_line
            LIMIT 50""",
        {"query": query, "repo": repo},
    ).fetchall()


def evidence_packet(
    db_path: Path,
    prompt: str,
    repo: str | None,
    max_bytes: int,
) -> dict[str, object]:
    """Return bounded, source-cited untrusted historical evidence."""
    if max_bytes < 1:
        raise ValueError("max bytes must be positive")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        matches = fetch_matches(connection, prompt, repo)
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
        item_size = len(json.dumps(item, ensure_ascii=False).encode())
        if item_size > max_bytes - used_bytes:
            continue
        evidence.append(item)
        used_bytes += item_size
    return {"evidence": evidence, "bytes": used_bytes, "untrusted": True}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--sessions-root", required=True, type=Path)
    build.add_argument("--db", required=True, type=Path)
    recall = commands.add_parser("recall")
    recall_group = recall.add_mutually_exclusive_group(required=True)
    recall_group.add_argument("--db", type=Path)
    recall_group.add_argument("--socket", type=Path)
    recall.add_argument("--prompt", required=True)
    recall.add_argument("--repo")
    recall.add_argument("--max-bytes", type=int, default=4000)
    serve = commands.add_parser("serve")
    serve.add_argument("--codex-root", required=True, type=Path)
    serve.add_argument("--claude-root", required=True, type=Path)
    serve.add_argument("--db", required=True, type=Path)
    serve.add_argument("--state-dir", required=True, type=Path)
    serve.add_argument("--socket", required=True, type=Path)
    serve.add_argument("--interval", type=float, default=1.0)
    for name in ("status", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--socket", required=True, type=Path)
    return parser.parse_args(argv)


def doctor_is_healthy(packet: Mapping[str, object]) -> bool:
    """Return whether a public status packet permits a zero doctor exit."""
    scanned = packet.get("last_scan")
    sources = packet.get("sources")
    if packet.get("available") is not True or not isinstance(scanned, float):
        return False
    if time.time() - scanned > 5 or packet.get("pending_sources"):
        return False
    if packet.get("last_error") or not isinstance(sources, Sequence):
        return False
    return not any(
        isinstance(source, Mapping)
        and (source.get("pending") or source.get("error"))
        for source in sources
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the builder or evidence retrieval command."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "build":
            print(
                json.dumps(
                    {"indexed": build_index(args.sessions_root, args.db)}
                )
            )
        elif args.command == "recall" and args.db:
            packet = evidence_packet(
                args.db,
                args.prompt,
                args.repo,
                args.max_bytes,
            )
            print(json.dumps(packet))
        else:
            from service import request, serve

            if args.command == "serve":
                if args.interval <= 0:
                    raise ValueError("interval must be positive")
                return serve(
                    args.codex_root,
                    args.claude_root,
                    args.db,
                    args.state_dir,
                    args.socket,
                    args.interval,
                )
            if args.command == "recall":
                print(
                    json.dumps(
                        request(
                            args.socket,
                            {
                                "op": "recall",
                                "prompt": args.prompt,
                                "repo": args.repo,
                                "max_bytes": args.max_bytes,
                            },
                        )
                    )
                )
            else:
                packet = request(args.socket, {"op": args.command})
                print(json.dumps(packet))
                if args.command == "doctor" and not doctor_is_healthy(packet):
                    return 1
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"context: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
