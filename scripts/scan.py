"""Incrementally reconcile raw session files into the derived store."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from normalizers import (
    discover,
    parse_source_incremental,
    source_digest,
    source_identity,
)
from wal_store import (
    advance_source,
    mark_issue,
    mark_missing,
    renamed_previous,
    source_cursor,
    source_rows,
)


def reconcile_sources(
    connection: sqlite3.Connection,
    codex_root: Path,
    claude_root: Path,
    migrated: bool,
    audit_index: int,
) -> tuple[bool, int, int]:
    """Reconcile changed sources and audit one unchanged source per pass."""
    known = source_rows(connection)
    sources = [
        (provider, root, source_path, path, fingerprint)
        for provider, root in (("codex", codex_root), ("claude", claude_root))
        for source_path, path, fingerprint in discover(root)
    ]
    current: set[tuple[str, str]] = set()
    changed = migrated
    indexed = 0
    audit_item = audit_index % len(sources) if sources else -1
    for index, (provider, root, source_path, path, fingerprint) in enumerate(
        sources
    ):
        current.add((provider, source_path))
        audit_digest = source_digest(path) if index == audit_item else None
        previous = known.get((provider, source_path)) or renamed_previous(
            connection,
            known,
            provider,
            source_path,
            source_identity(path),
            fingerprint,
            audit_digest,
        )
        source_changed, count = _reconcile_source(
            connection,
            provider,
            root,
            source_path,
            path,
            fingerprint,
            audit_digest,
            previous,
        )
        changed = changed or source_changed
        indexed += count
    with connection:
        changed = bool(mark_missing(connection, current)) or changed
    return changed, indexed, audit_index + int(bool(sources))


def _reconcile_source(
    connection: sqlite3.Connection,
    provider: str,
    root: Path,
    source_path: str,
    path: Path,
    fingerprint: str,
    audit_digest: str | None,
    previous: Mapping[str, object] | None,
) -> tuple[bool, int]:
    """Index one changed or audited source without advancing a bad cursor."""
    if (
        previous
        and previous["fingerprint"] == fingerprint
        and (audit_digest is None or previous["digest"] == audit_digest)
    ):
        return False, 0
    identity = source_identity(path)
    digest = audit_digest or source_digest(path)
    audited_change = bool(
        previous
        and audit_digest is not None
        and previous["fingerprint"] == fingerprint
        and previous["digest"] != audit_digest
    )
    offset, line, append = (
        (0, 0, False)
        if audited_change
        else source_cursor(previous, identity, path)
    )
    events, error, pending, end, end_line = parse_source_incremental(
        provider, root, path, offset, line
    )
    if error:
        with connection:
            mark_issue(
                connection,
                provider,
                source_path,
                identity,
                fingerprint,
                digest,
                error,
            )
        return True, 0
    advance_source(
        connection,
        provider,
        source_path,
        identity,
        fingerprint,
        digest,
        events,
        end,
        end_line,
        pending,
        not append,
    )
    return True, len(events)
