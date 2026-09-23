"""Build redacted health data for the provenance service."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def audit_bytes(state_dir: Path) -> int:
    """Return the current audit size without exposing its contents."""
    try:
        return (state_dir / "audit.jsonl").stat().st_size
    except OSError:
        return 0


def source_statuses(database: Path) -> tuple[list[dict[str, object]], bool]:
    """Return source health with redacted paths and legacy-read status."""
    if not database.exists():
        return [], False
    try:
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT provider, source_path, pending, status, error, "
                "last_seen FROM sources"
            )
            return [
                {
                    "provider": provider,
                    "source_id": hashlib.sha256(path.encode()).hexdigest()[
                        :16
                    ],
                    "pending": bool(pending),
                    "status": status,
                    "error": error,
                    "staleness_seconds": round(max(0, time.time() - seen), 3),
                }
                for provider, path, pending, status, error, seen in rows
            ], False
    except sqlite3.Error:
        return [], True


def unavailable_packet(reason: str = "unavailable") -> dict[str, object]:
    """Return a fail-closed recall packet with a redacted reason."""
    return {
        "available": False,
        "evidence": [],
        "bytes": 0,
        "untrusted": True,
        "unavailable_reason": reason,
    }
