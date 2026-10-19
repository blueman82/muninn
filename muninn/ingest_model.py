"""Constants and value types shared by the ingest modules."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from muninn import classify
from muninn.scope import CommitLookup

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
    """Invocation work totals across fresh ingest passes.

    Attributes:
        files_seen: Discovery visits, including repeated visits.
        files_changed: Source transactions committed, including replacements.
        events_added: Events inserted.
        events_removed: Events deleted by a replace.
        skipped_lines: Lines recorded as source issues.
        missing: Sources newly marked missing.
        duration_s: Outer wall time, including preparation and waits.
        skipped_files: Skipped visits: tombstones, duplicates, unidentified or
            vanished files, and failed opens (also ``unreadable_files``).
        unreadable_files: Failed open attempts (not vanished files).
        failed: Ordinary failed source read attempts; may later recover.
        errors: Exception class counts, excluding preparation outcomes.
    """

    files_seen: int = 0
    files_changed: int = 0
    events_added: int = 0
    events_removed: int = 0
    skipped_lines: int = 0
    missing: int = 0
    duration_s: float = 0.0
    skipped_files: int = 0
    unreadable_files: int = 0
    failed: int = 0
    errors: dict[str, int] = field(default_factory=dict[str, int])

    def accumulate(self, other: PassStats) -> None:
        """Add committed work and visits; the outer caller owns wall time."""
        self.files_seen += other.files_seen
        self.files_changed += other.files_changed
        self.events_added += other.events_added
        self.events_removed += other.events_removed
        self.skipped_lines += other.skipped_lines
        self.missing += other.missing
        self.skipped_files += other.skipped_files
        self.unreadable_files += other.unreadable_files
        self.failed += other.failed
        for kind, count in other.errors.items():
            self.errors[kind] = self.errors.get(kind, 0) + count


class IngestPreparationRequiredError(Exception):
    """End an incomplete pass with committed work and one missing Git fact."""

    def __init__(self, lookup: CommitLookup, stats: PassStats) -> None:
        """Preserve exact work totals before discarding this pass's state."""
        super().__init__("ingest_preparation_required")
        self.lookup = lookup
        self.stats = stats


class Written(NamedTuple):
    """The counts one source's transaction produced."""

    source_id: int
    added: int
    removed: int
    skipped: int


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
