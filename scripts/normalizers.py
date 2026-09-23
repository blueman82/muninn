"""Normalize safe Codex and Claude session records into cited events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from context import sanitize_text, text_content


def _metadata(record: Mapping[str, object]) -> dict[str, str]:
    """Return first supported provenance fields from a session record."""
    result: dict[str, str] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key in ("timestamp", "cwd", "gitBranch", "sessionId", "uuid"):
                item = value.get(key)
                if key not in result and isinstance(item, str) and item:
                    result[key] = item
            for item in value.values():
                if isinstance(item, (Mapping, list, tuple)):
                    visit(item)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes)
        ):
            for item in value:
                visit(item)

    visit(record)
    return result


def _codex_messages(value: object) -> Iterator[Mapping[str, object]]:
    """Yield only user and assistant message objects from a Codex record."""
    if isinstance(value, Mapping):
        if value.get("type") == "message" and value.get("role") in {
            "user",
            "assistant",
        }:
            yield value
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _codex_messages(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _codex_messages(child)


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


def _complete_lines(path: Path) -> tuple[list[tuple[int, bytes]], bool]:
    """Read terminated lines and retain an unfinished tail for retry."""
    raw = path.read_bytes()
    if not raw:
        return [], False
    complete = raw.endswith(b"\n")
    prefix = raw if complete else raw.rsplit(b"\n", 1)[0] + b"\n"
    return list(enumerate(prefix.splitlines(), start=1)), not complete


def parse_source(
    provider: str,
    root: Path,
    path: Path,
) -> tuple[list[dict[str, str | int | None]], str | None, bool]:
    """Parse one JSONL source with recoverable error and tail state."""
    source_key = str(path.relative_to(root))
    lines, pending = _complete_lines(path)
    events: list[dict[str, str | int | None]] = []
    for line, raw in lines:
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return (
                [],
                f"invalid_json_line_{line}:{type(error).__name__}",
                pending,
            )
        if not isinstance(record, Mapping):
            continue
        metadata = _metadata(record)
        if provider == "codex":
            messages = _codex_messages(record)
            texts = (
                (message.get("role"), text_content(message.get("content")))
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
            texts = [(role, _claude_text(message.get("content")))]
        for ordinal, (role, text) in enumerate(texts, start=1):
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
                        metadata,
                        role,
                        safe,
                    )
                )
    return events, None, pending


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


def discover(root: Path) -> Iterator[tuple[str, Path, str]]:
    """Yield source-keyed JSONL paths with their current fingerprints."""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_file():
            yield str(path.relative_to(root)), path, source_fingerprint(path)
