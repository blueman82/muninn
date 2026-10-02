"""Constants and value types shared by the ingest modules."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pctx import classify

# A decoded JSON object.  Transcript records come from other tools and have
# no schema we control, so their values are genuinely untyped; classify and
# ingest check each field's type before using it.
type Record = dict[str, Any]

# Longest line ingest will hold in memory; a longer one is recorded as a
# source issue and skipped.  Read as `ingest_model.MAX_LINE_BYTES` (never
# imported by name) so a test can lower it in one place.
MAX_LINE_BYTES = 8 * 1024 * 1024
PROVIDER = {
    "codex-sessions": "codex",
    "codex-archived": "codex",
    "claude-projects": "claude",
}
PATTERN = {
    "codex-sessions": "rollout-*.jsonl",
    "codex-archived": "rollout-*.jsonl",
    "claude-projects": "*.jsonl",
}
# Only primary and subagent threads get events; a reviewer or other thread
# keeps a source row (so it is known and not re-read) but no events.
INDEXED = ("primary", "subagent")


@dataclass
class PassStats:
    """Counters for one ingest pass.

    Attributes:
        files_seen: Regular transcript files found under the roots.
        files_changed: Sources committed this pass.
        events_added: Events inserted.
        events_removed: Events deleted by a replace.
        skipped_lines: Lines recorded as source issues.
        missing: Sources newly marked missing.
        duration_s: Wall time of the pass.
        skipped_files: Tombstoned, unidentifiable or duplicate files.
        failed: Sources rolled back by an error; redone next pass.
        errors: Exception class name to count, for the failed sources.
    """

    files_seen: int = 0
    files_changed: int = 0
    events_added: int = 0
    events_removed: int = 0
    skipped_lines: int = 0
    missing: int = 0
    duration_s: float = 0.0
    skipped_files: int = 0
    failed: int = 0
    errors: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(frozen=True)
class PlanOptions:
    """What one pass was asked to do.

    Attributes:
        roots: Provider root name to directory.
        full: Re-read every source from the start.
        only_threads: Thread or session ids to limit the pass to; None for
            all.
    """

    roots: Mapping[str, Path]
    full: bool = False
    only_threads: set[str] | None = None


@dataclass
class Work:
    """One source that needs writing, as decided by planning.

    Attributes:
        name: Provider root name the file was found under.
        path: File path under the provider root.
        rel: Path relative to the root, as stored in the source row.
        st: The lstat taken when the file was listed.
        info: Thread identity read from line 1.
        row: The existing source row, or None for a new source.
        first: Raw line 1, including its newline.
        mode: ``new``, ``append`` (resume at the cursor) or ``replace``.
    """

    name: str
    path: Path
    rel: str
    st: os.stat_result
    info: classify.ThreadInfo
    row: sqlite3.Row | None
    first: bytes
    mode: str
