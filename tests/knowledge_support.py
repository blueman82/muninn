"""Shared fixtures for the knowledge test modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pctx import knowledge
from tests.query_support import QueryCase

__all__ = ["CALL", "PROMPT", "REPLY", "ROOT", "SECRET", "KnowCase", "kid"]

ROOT = Path(__file__).resolve().parent.parent

PROMPT = "We decided to use the zebra cache for every lookup."
REPLY = "Understood, I will wire the zebra cache into the lookup path."
CALL = "Bash: pytest -q tests/test_lookup.py"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"


class KnowCase(QueryCase):
    """A repo scope with one primary thread: a prompt, a reply, a call."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")
        self.src = self.add_source("thr-main", session="sess-main")
        self.prompt = self.add_event(
            self.src, self.repo, PROMPT, ts="2026-09-01T10:00:00.000Z"
        )
        self.reply = self.add_event(
            self.src, self.repo, REPLY, kind="reply", role="assistant",
            ts="2026-09-01T10:00:05.000Z",
        )  # fmt: skip
        self.call = self.add_event(
            self.src, self.repo, CALL, kind="tool_call", role="assistant",
            tag="Bash", ts="2026-09-01T10:00:09.000Z",
        )  # fmt: skip

    # The builders return ``cursor.lastrowid``, which is typed as optional
    # but is always set after an INSERT; narrow it once here.
    def add_scope(
        self, key: str = "/repo", kind: str = "git", cwd: str | None = None
    ) -> int:
        """Insert a scope and return its id.

        Args:
            key: Scope key.
            kind: Scope kind.
            cwd: Working directory cached for the scope; defaults to key.

        Returns:
            Primary key of the scope row.
        """
        rowid = super().add_scope(key, kind, cwd)
        assert rowid is not None
        return rowid

    def add_source(
        self, thread: str = "t1", session: str | None = None, **kw: Any
    ) -> int:
        """Insert a source and return its id.

        Args:
            thread: Thread id of the source.
            session: Session id; defaults to the thread id.
            **kw: Extra source fields.

        Returns:
            Primary key of the source row.
        """
        rowid = super().add_source(thread, session, **kw)
        assert rowid is not None
        return rowid

    def add_event(
        self, source: int, scope_id: int, text: str, **kw: Any
    ) -> int:
        """Insert an event and return its id.

        Args:
            source: Primary key of the owning source.
            scope_id: Primary key of the owning scope.
            text: Event text.
            **kw: Extra event fields.

        Returns:
            Primary key of the event row.
        """
        rowid = super().add_event(source, scope_id, text, **kw)
        assert rowid is not None
        return rowid

    def ref(self, event_id: int) -> str:
        """Return the ``provider:thread:line.part`` ref of an event row.

        Args:
            event_id: Primary key of the event.

        Returns:
            The citation ref string for that event.
        """
        row = self.rw.execute(
            "SELECT s.provider, s.thread_id, e.line, e.part FROM event e"
            " JOIN source s ON s.id = e.source_id WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        return f"{row[0]}:{row[1]}:{row[2]}.{row[3]}"

    def add(self, **kw: Any) -> dict[str, Any]:
        """Call ``knowledge.add`` with defaults for a repo decision.

        Args:
            **kw: Arguments that override the defaults.

        Returns:
            The result of ``knowledge.add``.
        """
        args: dict[str, Any] = {
            "kind": "decision",
            "text": "Use the zebra cache for lookups",
            "cites": [(self.ref(self.prompt), "use the zebra cache")],
            "quote_only": None,
            "supersedes": None,
            "global_scope": False,
            "cwd": "/repo",
            "actor": "claude:abc123",
            "roots": {},
            "env": {},
        }
        return knowledge.add(self.rw, **(args | kw))

    def counts(self) -> dict[str, int]:
        """Return the row count of every table a refused add must not touch.

        Returns:
            Row counts keyed by table name.
        """
        tables = ("knowledge", "citation", "knowledge_log", "scope")
        return {
            t: self.rw.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in (*tables, "scope_path")
        }

    def refused(self, code: str, **kw: Any) -> knowledge.RefusedError:
        """Assert that ``add`` refuses with ``code`` and writes nothing.

        Args:
            code: The expected refusal code.
            **kw: Arguments that override the ``add`` defaults.

        Returns:
            The raised refusal, for further assertions.
        """
        before = self.counts()
        with self.assertRaises(knowledge.RefusedError) as caught:
            self.add(**kw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.counts(), before)
        return caught.exception

    def reparse(
        self, event_id: int, digest: str = "h2", text: str = PROMPT
    ) -> int:
        """Replace an event the way a replace-mode ingest pass does.

        The line's events are deleted and re-created, here with another
        hash or text.

        Args:
            event_id: Primary key of the event to replace.
            digest: Line hash stored on the new event.
            text: Text stored on the new event.

        Returns:
            Primary key of the new event.
        """
        old = self.rw.execute(
            "SELECT source_id, scope_id, line, part, ts FROM event"
            " WHERE id = ?",
            (event_id,),
        ).fetchone()
        self.rw.execute("DELETE FROM event WHERE id = ?", (event_id,))
        return self.add_event(
            old["source_id"], old["scope_id"], text, line=old["line"],
            part=old["part"], ts=old["ts"], digest=digest,
        )  # fmt: skip

    def other_event(self, text: str, cls: str = "primary", **kw: Any) -> int:
        """Add an event in a fresh thread of another thread class.

        Args:
            text: Text of the new event.
            cls: Thread class of the new source.
            **kw: Extra event fields.

        Returns:
            Primary key of the new event.
        """
        name = f"thr-{cls}-{self.rw.total_changes}"
        src = self.add_source(name, cls=cls)
        return self.add_event(src, self.repo, text, **kw)


def kid(got: dict[str, Any]) -> int:
    """Return the numeric id of the entry in an ``add`` result.

    Args:
        got: A result carrying ``entry.id`` such as ``K12``.

    Returns:
        The id without its ``K`` prefix.
    """
    return int(got["entry"]["id"][1:])
