"""Shared fixtures for the erase test modules.

Synthetic provider trees and a temp MUNINN_HOME only; knowledge rows are
committed from selected physical originals. Canaries are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from muninn import erase
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from tests.knowledge_retirement_support import window_note
from tests.test_ingest import IngestCase

CANARY = "CANARY-ERASE-" + "q7" * 10  # 33 chars, one token


def digest(path: Path) -> str:
    """Hash a file so a test can prove it was left untouched.

    Args:
        path: File to hash.

    Returns:
        The hex SHA-256 of the file bytes.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EraseCase(IngestCase):
    """Ingest fixture plus erase helpers over a temp home and roots.

    Attributes:
        env: Environment that points the erase at the synthetic roots.
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = {
            "HOME": str(self.tmp / "userhome"),
            "MUNINN_ROOTS": json.dumps(
                {k: str(v) for k, v in self.roots.items()}
            ),
        }

    def erase(
        self,
        *,
        session: str | None = None,
        event_ref: str | None = None,
        match: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Run an erase against the fixture store.

        Args:
            session: Session root id to erase.
            event_ref: ``provider:thread:line.part`` of one event.
            match: Literal text; every text containing it is erased.
            dry_run: Report the counts without changing anything.

        Returns:
            The erase report.
        """
        return erase.erase(
            self.conn,
            home=self.home,
            env=self.env,
            session=session,
            event_ref=event_ref,
            match=match,
            dry_run=dry_run,
        )

    def count(self, sql: str, *args: str | int) -> int:
        """Run a single-value query.

        Args:
            sql: Query whose first column of the first row is returned.
            *args: Bound parameters.

        Returns:
            The first column of the first row.
        """
        row: sqlite3.Row = self.conn.execute(sql, args).fetchone()
        return row[0]

    def note(
        self,
        text: str,
        thread: str,
        line: int = 2,
        *,
        additional: tuple[tuple[str, int], ...] = (),
        **updates: object,
    ) -> str:
        """Commit supported content from actual ingested fixture originals."""
        windows = []
        for thread_id, number in ((thread, line), *additional):
            row = self.conn.execute(
                "SELECT e.line_sha256 FROM event e JOIN source s "
                "ON s.id=e.source_id WHERE s.provider='codex' "
                "AND s.thread_id=? AND e.line=? AND e.part=1",
                (thread_id, number),
            ).fetchone()
            windows.append(
                EvidenceWindow(
                    source=SourceRef(provider="codex", thread_id=thread_id),
                    from_line=number,
                    through_line=number,
                    boundary_hash=row[0],
                )
            )
        return window_note(
            self.home,
            self.roots,
            windows[0],
            text,
            max_bytes=65536,
            allow_global=False,
            extra_windows=tuple(windows[1:]),
            kind="fact",
            **updates,
        )
