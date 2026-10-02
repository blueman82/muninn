"""Shared fixtures for the erase test modules.

Synthetic provider trees and a temp MUNINN_HOME only; knowledge rows are
inserted directly.  Canaries are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from muninn import erase
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

    def knowledge(
        self, text: str, cites: list[tuple[str, int, int, str]]
    ) -> int:
        """Insert a current knowledge entry with citations.

        Args:
            text: Entry text.
            cites: ``(thread_id, line, part, quote)`` for each cited event.

        Returns:
            The new knowledge id.
        """
        sid = self.count("SELECT id FROM scope LIMIT 1")
        kid = self.conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', ?, 'current', 'user', 0)",
            (sid, text),
        ).lastrowid
        assert kid is not None
        for thread, line, part, quote in cites:
            self.conn.execute(
                "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
                " part, line_sha256, role, kind, quote, span_start, span_end)"
                " VALUES (?, 'codex', ?, ?, ?, 'h', 'user', 'prompt', ?,"
                " 0, 1)",
                (kid, thread, line, part, quote),
            )
        return kid
