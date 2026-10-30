"""Hold the read-only selection for one governed erasure operation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from muninn.knowledge_erase_state import SupportClosure
from muninn.tombstone_intent import TombstoneRow


@dataclass
class Target:
    """Everything one erase request will remove or list."""

    sessions: set[tuple[str, str]] = field(
        default_factory=set[tuple[str, str]]
    )
    # Source rows are deleted; touched ones (a superset) are only reported.
    sources: dict[int, sqlite3.Row] = field(
        default_factory=dict[int, sqlite3.Row]
    )
    touched: dict[int, sqlite3.Row] = field(
        default_factory=dict[int, sqlite3.Row]
    )
    lines: set[tuple[int, int]] = field(default_factory=set[tuple[int, int]])
    events: list[int] = field(default_factory=list[int])
    tombstones: list[TombstoneRow] = field(default_factory=list[TombstoneRow])
    # (provider, thread, role, text) of each content tombstone to write.
    # Kept as text, not tags, so planning (and a dry run) needs no key.
    content: list[tuple[str, str, str, str]] = field(
        default_factory=list[tuple[str, str, str, str]]
    )
    knowledge: set[str] = field(default_factory=set[str])
    review: SupportClosure = field(
        default_factory=lambda: SupportClosure(
            set(), set(), set(), set(), set(), set()
        )
    )
