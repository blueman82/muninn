"""Fresh activation waits for governed Cursor cleanup and all provider work."""

from __future__ import annotations

import json
import sqlite3
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

from install import cutover, steps_release
from muninn import (
    cursor_import,
    ingest,
    knowledge_erase,
    review_evidence,
    review_requests,
    store,
    tombstones,
)
from muninn import review_pause_state as state
from muninn.review_evidence_models import (
    EvidenceRecord,
    EvidenceWindow,
    SourceRef,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from tests import test_install_ingest
from tests.classify_claude_support import claude_rec, text_block
from tests.ingest_support import line
from tests.test_classify import codex_meta, reply, user_msg
from tests.test_review_shared_models import policy_data


def history(home: Path, cwd: Path) -> Path:
    """Write three actual provider fixtures with independent known counts."""
    claude = home / ".claude/projects/project/session.jsonl"
    codex = home / ".codex/archived_sessions/rollout-one.jsonl"
    for path in (claude, codex):
        path.parent.mkdir(parents=True, exist_ok=True)
    claude.write_bytes(
        b"".join(
            map(
                line,
                [
                    claude_rec("user", "synthetic prompt", cwd=str(cwd)),
                    claude_rec(
                        "assistant",
                        [text_block("synthetic reply")],
                        cwd=str(cwd),
                    ),
                ],
            )
        )
    )
    codex.write_bytes(
        b"".join(
            map(
                line,
                [
                    codex_meta("user", "one", cwd=str(cwd)),
                    user_msg(1, "first"),
                    reply(2, "second"),
                    user_msg(3, "third"),
                ],
            )
        )
    )
    cursor = home / "synthetic-cursor.sqlite"
    composer = {"fullConversationHeadersOnly": [{"bubbleId": "one"}]}
    with closing(sqlite3.connect(cursor)) as conn, conn:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO cursorDiskKV VALUES (?,?)",
            [
                ("composerData:one", json.dumps(composer)),
                (
                    "bubbleId:one:one",
                    json.dumps({"type": 1, "text": "synthetic"}),
                ),
            ],
        )
    return cursor


class FreshCursorReplayTests(unittest.TestCase):
    """Exercise real authenticated cleanup through the fresh installer seam."""

    def setUp(self) -> None:
        self.fixture = test_install_ingest.PreparedFreshTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.ctx, self.rec = self.fixture.ctx, self.fixture.record
        self.db = store.db_path(self.ctx.data)
        self.original = self.fixture.source.read_bytes()
        self.cursor_original = self.fixture.cursor.database.read_bytes()
        self.callers: list[sqlite3.Connection] = []
        self.auxiliary: list[sqlite3.Connection] = []
        self.lookups: list[Path] = []
        self.queries: list[str] = []
        self.selected: tuple[EvidenceRecord, ...] = ()
        self.window: EvidenceWindow | None = None
        self.work = self.ctx.data / "uncreated"
        self.failure = OSError("synthetic cleanup failure")
        self.open_failure = sqlite3.OperationalError("synthetic RO failure")

    def prepare(self, path: Path, cwd: str) -> cursor_import.PreparedImport:
        """Select real fixture originals outside the writer."""
        with self.assertRaises(BlockingIOError):
            state.acquire(self.ctx.data)
        with store.writer_lock(self.ctx.data, wait_s=0):
            pass
        prepared = self.real_prepare(path, cwd)
        with closing(store.connect_ro(self.db)) as conn:
            row = conn.execute(
                "SELECT line,line_sha256 FROM event ORDER BY line DESC LIMIT 1"
            ).fetchone()
        self.window = EvidenceWindow(
            source=SourceRef(provider="codex", thread_id="fresh"),
            from_line=2,
            through_line=row[0],
            boundary_hash=row[1],
        )
        self.selected = review_evidence.initial_evidence(
            self.ctx.data, self.window, 4096
        ).excerpts
        self.assertTrue(self.selected)
        return prepared

    def seed_attempt(self, conn: sqlite3.Connection) -> None:
        """Admit real fixture evidence and leave a durable governed attempt."""
        assert self.window is not None
        policy = ReviewPolicy.model_validate(
            policy_data()
            | {
                "projection_version": "permitted-fields-v2",
                "max_input_bytes": 4096,
            }
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE review_control SET admission=1,generation=1,"
            "policy_json=?,policy_digest=?,activated_at=10.0",
            (canonical_json(policy), digest(policy)),
        )
        request = review_requests.enqueue_review(
            conn, self.window, self.selected[0].scope, policy, "explicit", 11.0
        )
        for evidence in self.selected:
            data = evidence.model_dump(mode="json")
            for key in ("scope", "selector", "original_locator"):
                data[f"{key}_json"] = json.dumps(data.pop(key))
            conn.execute(
                f"INSERT INTO review_evidence({','.join(data)})"
                f" VALUES({','.join('?' for _ in data)})",
                tuple(data.values()),
            )
            conn.execute(
                "INSERT INTO review_input VALUES(?,?)",
                (request.id, evidence.id),
            )
        attempt = review_requests.claim_attempt(conn, request.id, 1, 12.0)
        conn.execute("UPDATE review_control SET admission=0")
        conn.execute("COMMIT")
        self.work = self.ctx.data / "review-work" / attempt.id
        self.work.mkdir(parents=True)
        (self.work / "result.json").write_text('{"synthetic":true}')
        (self.work / "prompt.txt").write_text("synthetic governed prompt")
        tombstones.append_tombstones(
            self.ctx.data,
            [tombstones.tombstone_row("codex", "thread", thread_id="fresh")],
        )

    def assert_scrubbed(self, conn: sqlite3.Connection) -> None:
        """Observe committed cancellation and scrub through the real caller."""
        self.assertFalse(conn.in_transaction)
        self.assertEqual(
            tuple(
                conn.execute(
                    "SELECT state,current_attempt_id,cancel_code"
                    " FROM review_request"
                ).fetchone()
            ),
            ("cancelled", None, "evidence_erased"),
        )
        self.assertEqual(
            conn.execute("SELECT outcome FROM review_attempt").fetchone()[0],
            "evidence_erased",
        )
        self.assertEqual(
            [
                tuple(row)
                for row in conn.execute(
                    "SELECT state,excerpt FROM review_evidence"
                )
            ],
            [("erased", None)] * len(self.selected),
        )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute(
                "SELECT group_concat(thread_id) FROM source "
                "WHERE provider='codex'"
            ).fetchone()[0],
            "gone",
        )

    @contextmanager
    def observe(
        self, caller: sqlite3.Connection, fault: str
    ) -> Generator[None]:
        """Observe RO authentication and inject bounded failures."""
        real_ro, connect, unlink = (
            store.connect_ro,
            sqlite3.connect,
            knowledge_erase.os.unlink,
        )

        def opened(
            database: str, *, uri: bool, isolation_level: None
        ) -> sqlite3.Connection:
            self.assertEqual(
                database, self.db.absolute().as_uri() + "?mode=ro"
            )
            if fault == "open":
                raise self.open_failure
            return connect(database, uri=uri, isolation_level=isolation_level)

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if (
                fault == "query"
                and action == sqlite3.SQLITE_READ
                and table == "review_attempt"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def readonly(path: Path) -> sqlite3.Connection:
            self.assertEqual(path, self.db)
            self.assert_scrubbed(caller)
            self.lookups.append(path)
            extra = real_ro(path)
            self.auxiliary.append(extra)
            extra.set_trace_callback(self.queries.append)
            extra.set_authorizer(authorize)
            return extra

        def removed(path: str, *, dir_fd: int | None = None) -> None:
            if fault == "cleanup":
                raise self.failure
            unlink(path, dir_fd=dir_fd)

        with (
            mock.patch.object(store, "connect_ro", side_effect=readonly),
            mock.patch.object(sqlite3, "connect", side_effect=opened),
            mock.patch.object(store, "connect_rw", side_effect=AssertionError),
            mock.patch.object(
                store, "writer_lock", side_effect=AssertionError
            ),
            mock.patch.object(
                cursor_import, "read", side_effect=AssertionError
            ),
            mock.patch.object(
                knowledge_erase.os, "unlink", side_effect=removed
            ),
            mock.patch("subprocess.Popen", side_effect=AssertionError),
        ):
            yield

    @contextmanager
    def operation(self, fault: str = "") -> Generator[None]:
        """Inject governed intent at the real final caller-owned apply seam."""
        self.real_prepare = cursor_import.prepare
        real_apply = cursor_import.apply

        def apply(
            conn: sqlite3.Connection,
            home: Path,
            prepared: cursor_import.PreparedImport,
        ) -> dict[str, int]:
            self.callers.append(conn)
            self.assertEqual(home, self.ctx.data)
            self.seed_attempt(conn)
            with self.assertRaises(BlockingIOError):
                state.acquire(home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(home, wait_s=0),
            ):
                self.fail("writer free at apply")
            try:
                with self.observe(conn, fault):
                    return real_apply(conn, home, prepared)
            finally:
                self.assert_scrubbed(conn)
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
                for extra in self.auxiliary:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        extra.execute("SELECT 1")

        roots = ingest.default_roots(
            {
                "HOME": str(self.ctx.home),
                "CODEX_HOME": str(self.ctx.codex_home),
                "CLAUDE_CONFIG_DIR": str(self.ctx.settings.parent),
            }
        )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "MUNINN_ROOTS": json.dumps(
                        {k: str(v) for k, v in roots.items()}
                    ),
                    "HOME": str(self.ctx.home),
                    "CODEX_HOME": str(self.ctx.codex_home),
                },
            ),
            mock.patch.object(
                cursor_import,
                "default_database",
                return_value=self.fixture.cursor.database,
            ),
            mock.patch.object(
                cursor_import, "prepare", side_effect=self.prepare
            ),
            mock.patch.object(cursor_import, "apply", side_effect=apply),
        ):
            yield

    def assert_final(self, *, success: bool) -> None:
        """Check durable scrub, cleanup, original bytes and release."""
        self.assertEqual(self.lookups, [self.db])
        self.assertEqual(len(self.callers), 1)
        for conn in self.callers + self.auxiliary:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        with closing(store.connect_ro(self.db)) as conn:
            self.assert_scrubbed(conn)
            count = conn.execute(
                "SELECT count(*) FROM source WHERE provider='cursor'"
            ).fetchone()[0]
            self.assertEqual(count, int(success))
        self.assertEqual(self.work.exists(), not success)
        self.assertEqual((self.ctx.data / "status.json").exists(), success)
        self.assertEqual(self.fixture.source.read_bytes(), self.original)
        self.assertEqual(
            self.fixture.cursor.database.read_bytes(), self.cursor_original
        )
        with store.writer_lock(self.ctx.data, wait_s=0):
            pass
        fd = state.acquire(self.ctx.data)
        knowledge_erase.os.close(fd)

    def test_real_cleanup_finishes_before_activation(self) -> None:
        with self.operation(), cutover.prepared_store(self.ctx, self.rec):
            self.assertEqual(
                self.rec["events_by_provider"], {"codex": 2, "cursor": 2}
            )
            self.assertFalse(self.work.exists())
        self.assertEqual(len(self.auxiliary), 1)
        self.assertIn("SELECT 1 FROM review_attempt", self.queries[0])
        self.assert_final(success=True)

    def test_auxiliary_open_failure_keeps_scrub_and_refuses_activation(
        self,
    ) -> None:
        with (
            self.operation("open"),
            mock.patch.object(steps_release, "pin") as activate,
        ):
            with (
                self.assertRaises(store.StoreUnavailableError) as caught,
                cutover.prepared_store(self.ctx, self.rec),
            ):
                steps_release.pin(self.ctx, self.rec)
            activate.assert_not_called()
        self.assertIs(caught.exception.__cause__, self.open_failure)
        self.assertEqual(self.auxiliary, [])
        self.assert_final(success=False)

    def test_auxiliary_query_failure_keeps_scrub_and_refuses_activation(
        self,
    ) -> None:
        with (
            self.operation("query"),
            mock.patch.object(steps_release, "pin") as activate,
        ):
            with (
                self.assertRaises(sqlite3.DatabaseError),
                cutover.prepared_store(self.ctx, self.rec),
            ):
                steps_release.pin(self.ctx, self.rec)
            activate.assert_not_called()
        self.assertEqual(len(self.auxiliary), 1)
        self.assert_final(success=False)

    def test_filesystem_failure_keeps_scrub_and_refuses_activation(
        self,
    ) -> None:
        with (
            self.operation("cleanup"),
            mock.patch.object(steps_release, "pin") as activate,
        ):
            with (
                self.assertRaises(OSError) as caught,
                cutover.prepared_store(self.ctx, self.rec),
            ):
                steps_release.pin(self.ctx, self.rec)
            activate.assert_not_called()
        self.assertIs(caught.exception, self.failure)
        self.assertEqual(len(self.auxiliary), 1)
        self.assert_final(success=False)
