"""Normalize safe Codex and Claude session records into cited events."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from context import sanitize_text, text_content

MAX_SOURCE_LINE_BYTES = 1_048_576
MAX_SOURCE_DEPTH = 1_000


def _within_depth(value: object) -> bool:
    """Reject deeply nested source data before it reaches recursive parsing."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_SOURCE_DEPTH:
            return False
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            pending.extend((child, depth + 1) for child in item)
    return True


def _metadata(record: Mapping[str, object]) -> dict[str, str]:
    """Return direct provenance fields from a trusted record container."""
    return {
        key: value
        for key in ("timestamp", "cwd", "gitBranch", "sessionId", "uuid")
        if isinstance(value := record.get(key), str) and value
    }


def _codex_messages(
    record: Mapping[str, object],
) -> Iterator[Mapping[str, object]]:
    """Yield only direct Codex response messages, never nested tool data."""
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


def _claude_text(content: object) -> str:
    """Return text blocks while excluding thinking and tool content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    blocks = [
        text_content(block)
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(block for block in blocks if block)


def _event(
    provider: str,
    source_key: str,
    line: int,
    ordinal: int,
    raw: bytes,
    metadata: Mapping[str, str],
    role: str,
    text: str,
) -> dict[str, str | int | None]:
    """Build one normalized event with a source-stable identity."""
    return {
        "provider": provider,
        "source_path": source_key,
        "source_line": line,
        "source_ordinal": ordinal,
        "source_hash": hashlib.sha256(raw).hexdigest(),
        "timestamp": metadata.get("timestamp"),
        "cwd": metadata.get("cwd"),
        "repo": metadata.get("cwd"),
        "role": role,
        "text": text,
    }


def _complete_lines(
    path: Path, offset: int = 0, line_start: int = 0
) -> tuple[list[tuple[int, bytes]], bool, int]:
    """Read complete lines after an already committed byte cursor."""
    lines: list[tuple[int, bytes]] = []
    with path.open("rb") as source:
        source.seek(offset)
        number = line_start
        while raw := source.readline():
            number += 1
            if len(raw) > MAX_SOURCE_LINE_BYTES:
                raise ValueError("source_line_too_large")
            if raw.endswith(b"\n"):
                lines.append((number, raw.rstrip(b"\r\n")))
            else:
                try:
                    json.loads(raw)
                except (
                    RecursionError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ):
                    return lines, True, source.tell() - len(raw)
                lines.append((number, raw))
        end = source.tell()
    return lines, False, end


def parse_source(
    provider: str,
    root: Path,
    path: Path,
) -> tuple[list[dict[str, str | int | None]], str | None, bool]:
    """Parse one JSONL source with recoverable error and tail state."""
    source_key = str(path.relative_to(root))
    try:
        lines, pending, _ = _complete_lines(path)
    except (OSError, ValueError) as error:
        return [], type(error).__name__, True
    events: list[dict[str, str | int | None]] = []
    for line, raw in lines:
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (
            RecursionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            return (
                [],
                f"invalid_json_line_{line}:{type(error).__name__}",
                pending,
            )
        if not isinstance(record, Mapping):
            continue
        if not _within_depth(record):
            return [], f"nested_source_line_{line}", pending
        try:
            metadata = _metadata(record)
            if provider == "codex":
                messages = _codex_messages(record)
                texts = (
                    (
                        message.get("role"),
                        text_content(message.get("content")),
                        metadata | _metadata(message),
                    )
                    for message in messages
                )
            else:
                message = record.get("message")
                role = record.get("type")
                if not isinstance(message, Mapping) or role not in {
                    "user",
                    "assistant",
                }:
                    continue
                message_role = message.get("role")
                if message_role not in {role, None}:
                    continue
                texts = [
                    (
                        role,
                        _claude_text(message.get("content")),
                        metadata | _metadata(message),
                    )
                ]
        except RecursionError:
            return [], f"nested_source_line_{line}", pending
        for ordinal, (role, text, event_metadata) in enumerate(texts, start=1):
            if not isinstance(role, str):
                continue
            if safe := sanitize_text(text):
                events.append(
                    _event(
                        provider,
                        source_key,
                        line,
                        ordinal,
                        raw,
                        event_metadata,
                        role,
                        safe,
                    )
                )
    return events, None, pending


def parse_source_incremental(
    provider: str,
    root: Path,
    path: Path,
    offset: int,
    line_start: int,
) -> tuple[list[dict[str, str | int | None]], str | None, bool, int, int]:
    """Parse complete bytes after a committed cursor."""
    source_key = str(path.relative_to(root))
    try:
        lines, pending, complete_end = _complete_lines(
            path, offset, line_start
        )
    except (OSError, ValueError) as error:
        return [], type(error).__name__, True, offset, line_start
    events: list[dict[str, str | int | None]] = []
    for line, raw in lines:
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (
            RecursionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            return (
                [],
                f"invalid_json_line_{line}:{type(error).__name__}",
                pending,
                offset,
                line_start,
            )
        if not isinstance(record, Mapping):
            continue
        if not _within_depth(record):
            return (
                [],
                f"nested_source_line_{line}",
                pending,
                offset,
                line_start,
            )
        try:
            metadata = _metadata(record)
            if provider == "codex":
                messages = _codex_messages(record)
                texts = (
                    (
                        message.get("role"),
                        text_content(message.get("content")),
                        metadata | _metadata(message),
                    )
                    for message in messages
                )
            else:
                message = record.get("message")
                role = record.get("type")
                if not isinstance(message, Mapping) or role not in {
                    "user",
                    "assistant",
                }:
                    continue
                message_role = message.get("role")
                if message_role not in {role, None}:
                    continue
                texts = [
                    (
                        role,
                        _claude_text(message.get("content")),
                        metadata | _metadata(message),
                    )
                ]
        except RecursionError:
            return (
                [],
                f"nested_source_line_{line}",
                pending,
                offset,
                line_start,
            )
        for ordinal, (role, text, event_metadata) in enumerate(texts, start=1):
            if isinstance(role, str) and (safe := sanitize_text(text)):
                events.append(
                    _event(
                        provider,
                        source_key,
                        line,
                        ordinal,
                        raw,
                        event_metadata,
                        role,
                        safe,
                    )
                )
    last_line = lines[-1][0] if lines else line_start
    return events, None, pending, complete_end, last_line


def source_fingerprint(path: Path) -> str:
    """Return an identity that detects append, replacement, and truncation."""
    stat = path.stat()
    return ":".join(
        str(value)
        for value in (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    )


def source_identity(path: Path) -> str:
    """Return identity that distinguishes append from replacement."""
    source_stat = path.stat()
    return f"{source_stat.st_dev}:{source_stat.st_ino}"


def source_digest(path: Path) -> str:
    """Return a content digest for amortized edit audits."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1_048_576):
            digest.update(block)
    return digest.hexdigest()


def discover(root: Path) -> Iterator[tuple[str, Path, str]]:
    """Yield source-keyed JSONL paths with their current fingerprints."""
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise OSError("configured_source_root_unavailable")
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_file() and not path.is_symlink():
            yield str(path.relative_to(root)), path, source_fingerprint(path)
