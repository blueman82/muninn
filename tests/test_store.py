"""Store contract: schema v1, pragmas, journal mode, locking, private files.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

import json
import os
import secrets
import select
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pctx import store

ROOT = Path(__file__).resolve().parent.parent

# Child scripts: argv[1] is the repo root, the rest are test arguments.
HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pctx import store
with store.writer_lock(Path(sys.argv[2]), wait_s=0):
    print("ready", flush=True)
    time.sleep(float(sys.argv[3]))
"""

# A writer killed mid-transaction after its cache spilled to the db file:
# the journal is synced, so it is genuinely hot.
SPILLING_WRITER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pctx import store
conn = store.connect_rw(Path(sys.argv[2]))
conn.execute("PRAGMA cache_size=8")
conn.execute("BEGIN IMMEDIATE")
for i in range(400):
    conn.execute(
        "INSERT INTO scope(key, label, kind) VALUES (?, ?, 'dir')",
        (f"/uncommitted/{i}", "x" * 900),
    )
print("ready", flush=True)
time.sleep(120)
"""


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def insert_scope(conn, key="/repo", kind="git"):
    label = key.rsplit("/", 1)[-1] or key
    return conn.execute(
        "INSERT INTO scope(key, label, kind) VALUES (?, ?, ?)",
        (key, label, kind),
    ).lastrowid


def insert_source(conn, thread_id="t1", thread_class="primary"):
    return conn.execute(
        "INSERT INTO source(provider, thread_id, session_root,"
        " thread_class, class_reason, replay_mode, root, path,"
        " first_line_sha256, ino, size, mtime_ns, classifier_version,"
        " first_seen, last_seen)"
        " VALUES ('codex', ?, ?, ?, 'test', 'none', 'codex-sessions', ?,"
        " 'h', 1, 1, 1, 1, 0, 0)",
        (thread_id, thread_id, thread_class, f"{thread_id}.jsonl"),
    ).lastrowid


def insert_event(conn, source_id, scope_id, **kw):
    row = {
        "line": 1,
        "part": 1,
        "role": "user",
        "kind": "prompt",
        "cwd": None,
        "parent": None,
        "text": "hello",
    } | kw
    return conn.execute(
        "INSERT INTO event(source_id, line, part, byte_offset,"
        " line_sha256, seq, role, kind, scope_id, cwd, parent_event_id,"
        " text) VALUES (?, ?, ?, 0, 'h', ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            row["line"],
            row["part"],
            row["line"],
            row["role"],
            row["kind"],
            scope_id,
            row["cwd"],
            row["parent"],
            row["text"],
        ),
    ).lastrowid


class Child:
    """A python subprocess that prints 'ready' once it holds its resource."""

    def __init__(self, case, code, *args):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT)]
            + [str(a) for a in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        case.addCleanup(self.kill)

    def wait_ready(self, timeout=30):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline() if ready else ""
        if line.strip() != "ready":
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            errors = self.proc.stderr.read()  # before kill() closes the pipe
            self.kill()
            raise AssertionError(f"child not ready: {errors}")

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.db = store.db_path(self.home)

    def rw(self, **kw):
        conn = store.connect_rw(self.db, **kw)
        self.addCleanup(conn.close)
        return conn

    def ro(self):
        conn = store.connect_ro(self.db)
        self.addCleanup(conn.close)
        return conn

    def residue(self, needles):
        """Occurrences of any needle in any file under the data dir."""
        return sum(
            p.read_bytes().count(n)
            for p in self.home.rglob("*")
            if p.is_file()
            for n in needles
        )


class PathTests(StoreCase):
    def test_data_home_env_default_and_expansion(self):
        default = Path.home() / ".local" / "share" / "provenance-context"
        self.assertEqual(store.data_home({"PCTX_HOME": "/x/y"}), Path("/x/y"))
        self.assertEqual(store.data_home({}), default)
        self.assertEqual(store.data_home({"PCTX_HOME": ""}), default)
        self.assertEqual(
            store.data_home({"PCTX_HOME": "~/pc"}), Path.home() / "pc"
        )

    def test_db_path(self):
        self.assertEqual(store.db_path(Path("/h")), Path("/h/pctx.sqlite"))

    def test_ensure_private_dir_creates_and_tightens(self):
        target = self.tmp / "a" / "b"
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)
        os.chmod(target, 0o755)
        store.ensure_private_dir(target)
        self.assertEqual(mode(target), 0o700)

    def test_write_json_atomic_is_private_and_replaces(self):
        target = self.tmp / "status.json"
        target.write_text("{}")
        os.chmod(target, 0o644)
        store.write_json_atomic(target, {"b": 2, "a": [1]})
        self.assertEqual(target.read_text(), '{"a": [1], "b": 2}')  # sorted
        self.assertEqual(mode(target), 0o600)

    def test_write_json_atomic_failure_keeps_old_and_leaves_no_temp(self):
        target = self.tmp / "status.json"
        store.write_json_atomic(target, {"ok": True})
        with self.assertRaises(TypeError):  # unserialisable: no temp file
            store.write_json_atomic(target, {"x": object()})
        blocker = self.tmp / "blocked"
        blocker.mkdir()  # os.replace onto a directory fails after writing
        with self.assertRaises(OSError):
            store.write_json_atomic(blocker, {"ok": True})
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual(
            sorted(os.listdir(self.tmp)), ["blocked", "status.json"]
        )


class SchemaTests(StoreCase):
    def test_connect_rw_creates_schema_version_1(self):
        conn = self.rw()
        self.assertEqual(store.SCHEMA_VERSION, 1)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        found = {
            (r["type"], r["name"])
            for r in conn.execute(
                "SELECT type, name FROM sqlite_master"
                " WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            )
        }
        tables = {
            "scope",
            "scope_path",
            "source",
            "source_issue",
            "event",
            "event_fts",
            "knowledge",
            "knowledge_fts",
            "citation",
            "knowledge_log",
            "tombstone",
        }
        indexes = {
            "source_session",
            "event_scope_ts",
            "event_cwd",
            "citation_target",
            "tombstone_session",
            "tombstone_thread",
        }
        triggers = {
            "event_ai",
            "event_ad",
            "event_immutable",
            "knowledge_ai",
            "knowledge_ad",
            "knowledge_au",
            "knowledge_log_no_update",
            "knowledge_log_no_delete",
        }
        self.assertLessEqual({("table", t) for t in tables}, found)
        self.assertEqual({n for t, n in found if t == "index"}, indexes)
        self.assertEqual({n for t, n in found if t == "trigger"}, triggers)

    def test_connect_rw_reopen_keeps_data(self):
        first = self.rw()
        insert_scope(first, "/keep")
        first.close()
        again = self.rw()
        self.assertEqual(again.execute("PRAGMA user_version").fetchone()[0], 1)
        keys = [r["key"] for r in again.execute("SELECT key FROM scope")]
        self.assertEqual(keys, ["/keep"])

    def test_failed_schema_creation_rolls_back(self):
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("CREATE TABLE tombstone (x)")  # collides with the last
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            store.connect_rw(self.db)
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        tables = [
            r[0]
            for r in raw.execute("SELECT name FROM sqlite_master")
            if r[0] != "sqlite_sequence"
        ]
        self.assertEqual(tables, ["tombstone"])
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_connect_rw_refuses_newer_schema(self):
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version=2")
        raw.close()
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_rw(self.db)

    def test_journal_mode_is_delete(self):
        conn = self.rw()
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
        )
        insert_scope(conn)
        self.assertEqual(
            sorted(os.listdir(self.home)), ["pctx.sqlite"]
        )  # no -wal, -shm or leftover -journal after a write

    def test_connect_rw_refuses_wal_mode(self):
        store.ensure_private_dir(self.home)
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE x (y)")
        raw.commit()
        raw.close()
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_rw(self.db)

    def test_writer_pragmas(self):
        conn = self.rw()

        def pragma(name):
            return conn.execute(f"PRAGMA {name}").fetchone()[0]

        self.assertEqual(pragma("journal_mode"), "delete")
        self.assertEqual(pragma("secure_delete"), 1)
        self.assertEqual(pragma("foreign_keys"), 1)
        self.assertEqual(pragma("busy_timeout"), 5000)
        self.assertEqual(pragma("synchronous"), 2)
        self.assertEqual(pragma("cache_size"), -262144)
        self.assertEqual(pragma("fullfsync"), 1)

    def test_fullfsync_can_be_disabled(self):
        conn = self.rw(fullfsync=False)
        self.assertEqual(conn.execute("PRAGMA fullfsync").fetchone()[0], 0)

    def test_private_modes(self):
        old = os.umask(0o022)  # permissive, so 0600/0700 are ours, not umask
        self.addCleanup(os.umask, old)
        conn = self.rw()
        conn.execute("BEGIN IMMEDIATE")
        insert_scope(conn)
        self.assertEqual(mode(f"{self.db}-journal"), 0o600)
        conn.execute("COMMIT")
        with store.writer_lock(self.home):
            self.assertEqual(mode(self.home / "writer.lock"), 0o600)
        self.assertEqual(mode(self.home), 0o700)
        self.assertEqual(mode(self.db), 0o600)

    def test_lax_existing_db_is_tightened(self):
        store.ensure_private_dir(self.home)
        self.db.touch()
        os.chmod(self.db, 0o644)
        self.rw()
        self.assertEqual(mode(self.db), 0o600)

    def test_missing_home_is_created_private_by_lock_and_connect(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with store.writer_lock(self.tmp / "via-lock", wait_s=0):
            pass
        conn = store.connect_rw(self.tmp / "via-connect" / "pctx.sqlite")
        self.addCleanup(conn.close)
        self.assertEqual(mode(self.tmp / "via-lock"), 0o700)
        self.assertEqual(mode(self.tmp / "via-connect"), 0o700)

    def test_event_cwd_column_and_index(self):
        conn = self.rw()
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(event)")}
        self.assertEqual(cols["cwd"]["type"], "TEXT")
        self.assertEqual(cols["cwd"]["notnull"], 0)
        index = [
            r["name"] for r in conn.execute("PRAGMA index_info(event_cwd)")
        ]
        self.assertEqual(index, ["cwd"])
        sid, src = insert_scope(conn), insert_source(conn)
        with_cwd = insert_event(conn, src, sid, line=1, cwd="/w/repo")
        no_cwd = insert_event(conn, src, sid, line=2)
        query = "SELECT id FROM event WHERE cwd = ?"
        plan = " ".join(
            r["detail"]
            for r in conn.execute("EXPLAIN QUERY PLAN " + query, ("/w/repo",))
        )
        self.assertIn("event_cwd", plan)
        hits = [r["id"] for r in conn.execute(query, ("/w/repo",))]
        self.assertEqual(hits, [with_cwd])
        row = conn.execute("SELECT cwd FROM event WHERE id = ?", (no_cwd,))
        self.assertIsNone(row.fetchone()["cwd"])

    def test_event_kinds_and_parent_link(self):
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        call = insert_event(conn, src, sid, line=1, kind="tool_call")
        for line, kind in enumerate(
            ("prompt", "reply", "harness", "delegation", "tool_error"), 2
        ):
            insert_event(conn, src, sid, line=line, kind=kind, parent=call)
        err = conn.execute(
            "SELECT parent_event_id FROM event WHERE kind = 'tool_error'"
        ).fetchone()
        self.assertEqual(err["parent_event_id"], call)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_event(conn, src, sid, line=9, kind="tool_output")

    def test_events_may_come_from_any_thread_class(self):
        conn = self.rw()
        sid = insert_scope(conn)
        for n, cls in enumerate(("primary", "subagent", "reviewer", "other")):
            src = insert_source(conn, f"t{n}", cls)
            insert_event(conn, src, sid, kind="delegation")
        count = conn.execute("SELECT count(*) FROM event").fetchone()[0]
        self.assertEqual(count, 4)

    def test_foreign_keys_enforced(self):
        conn = self.rw()
        sid = insert_scope(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_event(conn, 999, sid)

    def test_constraints_reject_invalid_rows(self):
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        bad = {
            "scope kind": (
                "INSERT INTO scope(key, label, kind) VALUES ('/a', 'a', 'x')"
            ),
            "scope_path method": (
                "INSERT INTO scope_path(cwd, scope_id, method)"
                f" VALUES ('/a', {sid}, 'x')"
            ),
            "source thread_class": ("UPDATE source SET thread_class = 'x'"),
            "source_issue code": (
                "INSERT INTO source_issue(source_id, line, at, code)"
                f" VALUES ({src}, 1, 0, 'x')"
            ),
            "event role": (
                "INSERT INTO event(source_id, line, part, byte_offset,"
                " line_sha256, seq, role, kind, scope_id, text) VALUES"
                f" ({src}, 1, 1, 0, 'h', 1, 'developer', 'prompt', {sid}, 't')"
            ),
            "knowledge text null unless erased": (
                "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
                f" created_at) VALUES ({sid}, 'fact', NULL, 'current', 'u', 0)"
            ),
            "tombstone session without root": (
                "INSERT INTO tombstone(created_at, provider, level)"
                " VALUES (0, 'codex', 'session')"
            ),
            "tombstone line without hash": (
                "INSERT INTO tombstone(created_at, provider, level,"
                " thread_id, line) VALUES (0, 'codex', 'line', 't', 1)"
            ),
        }
        for name, sql in bad.items():
            with self.subTest(name):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
        conn.execute(  # the same shapes are accepted when well formed
            "INSERT INTO tombstone(created_at, provider, level, session_root)"
            " VALUES (0, 'codex', 'session', 's')"
        )
        conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', NULL, 'erased', 'u', 0)",
            (sid,),
        )

    def test_event_rows_are_immutable(self):
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid)
        for column in ("text", "cwd", "parent_event_id", "flags"):
            with self.subTest(column):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "event is immutable"
                ):
                    conn.execute(
                        f"UPDATE event SET {column} = NULL WHERE id = ?",
                        (eid,),
                    )
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))  # erase path

    def test_knowledge_log_append_only(self):
        conn = self.rw()
        conn.execute(
            "INSERT INTO knowledge_log(knowledge_id, action, actor, at)"
            " VALUES (1, 'add', 'user', 0)"
        )
        for sql in (
            "UPDATE knowledge_log SET actor = 'x'",
            "DELETE FROM knowledge_log",
        ):
            with self.subTest(sql):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "append-only"
                ):
                    conn.execute(sql)

    def test_fts_secure_delete_enabled(self):
        conn = self.rw()
        for fts in ("event_fts", "knowledge_fts"):
            with self.subTest(fts):
                row = conn.execute(
                    f"SELECT v FROM {fts}_config WHERE k = 'secure-delete'"
                ).fetchone()
                self.assertEqual(row["v"], 1)

    def test_fts_triggers_keep_indexes_consistent(self):
        conn = self.rw()

        def hits(fts, term):
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?", (term,)
                )
            ]

        def check(fts):
            conn.execute(
                f"INSERT INTO {fts}({fts}, rank) VALUES ('integrity-check', 1)"
            )

        sid, src = insert_scope(conn), insert_source(conn)
        e1 = insert_event(conn, src, sid, line=1, text="alpha beta")
        e2 = insert_event(conn, src, sid, line=2, text="gamma delta")
        self.assertEqual(hits("event_fts", "alpha"), [e1])
        conn.execute("DELETE FROM event WHERE id = ?", (e1,))
        self.assertEqual(hits("event_fts", "alpha"), [])
        self.assertEqual(hits("event_fts", "gamma"), [e2])
        check("event_fts")

        def update(sql, *args):
            conn.execute(sql, args)

        kid = conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at)"
            " VALUES (?, 'fact', 'omega sigma', 'current', 'u', 0)",
            (sid,),
        ).lastrowid
        self.assertEqual(hits("knowledge_fts", "omega"), [kid])
        update("UPDATE knowledge SET status = 'superseded' WHERE id = ?", kid)
        self.assertEqual(hits("knowledge_fts", "omega"), [kid])
        update("UPDATE knowledge SET text = 'tau' WHERE id = ?", kid)
        self.assertEqual(hits("knowledge_fts", "omega"), [])
        self.assertEqual(hits("knowledge_fts", "tau"), [kid])
        update(
            "UPDATE knowledge SET text = NULL, status = 'erased' WHERE id = ?",
            kid,
        )
        self.assertEqual(hits("knowledge_fts", "tau"), [])
        # touching or deleting an already-erased row must not corrupt FTS
        update("UPDATE knowledge SET retract_reason = NULL WHERE id = ?", kid)
        update("DELETE FROM knowledge WHERE id = ?", kid)
        check("knowledge_fts")


class ErasureTests(StoreCase):
    def canary(self):
        token = secrets.token_hex(16)  # 32 ASCII bytes
        # the whole token plus both ends: FTS5 prefix-compresses terms
        return token, [t.encode() for t in (token, token[:12], token[-12:])]

    def test_erased_canary_leaves_zero_bytes(self):
        token, needles = self.canary()
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid, text=f"before {token} after")
        self.assertGreater(self.residue(needles), 0)  # the search can hit
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))
        conn.close()
        self.assertEqual(self.residue(needles), 0)

    def test_canary_control_leaves_residue_without_secure_settings(self):
        token, needles = self.canary()
        conn = self.rw()
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute(
            "INSERT INTO event_fts(event_fts, rank)"
            " VALUES ('secure-delete', 0)"
        )
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid, text=f"before {token} after")
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))
        conn.close()
        self.assertGreater(self.residue(needles), 0)  # the test can fail

    def test_erased_knowledge_and_quote_leave_zero_bytes(self):
        token, needles = self.canary()
        conn = self.rw()
        sid = insert_scope(conn)
        kid = conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', ?, 'current', 'u', 0)",
            (sid, f"note {token}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind, quote)"
            " VALUES (?, 'codex', 't', 1, 1, 'h', 'user', 'prompt', ?)",
            (kid, f"quote {token}"),
        )
        self.assertGreater(self.residue(needles), 0)
        conn.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased'"
        )
        conn.execute(
            "UPDATE knowledge SET text = NULL, status = 'erased'"
            " WHERE id = ?",
            (kid,),
        )
        conn.close()
        self.assertEqual(self.residue(needles), 0)


class ReaderTests(StoreCase):
    def test_connect_ro_missing_raises_store_unavailable(self):
        with self.assertRaises(store.StoreUnavailableError):
            store.connect_ro(self.db)
        self.assertFalse(self.db.exists())

    def assert_plain_unavailable(self):
        with self.assertRaises(store.StoreUnavailableError) as caught:
            store.connect_ro(self.db)
        self.assertNotIsInstance(caught.exception, store.HotJournalError)

    def test_connect_ro_rejects_uninitialised_and_foreign_schema(self):
        store.ensure_private_dir(self.home)
        self.db.touch()  # zero bytes: no writer has created the schema yet
        self.assert_plain_unavailable()
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version=2")  # written by a newer pctx
        raw.close()
        self.assert_plain_unavailable()

    def test_connect_ro_is_query_only(self):
        self.rw().close()
        conn = self.ro()
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000
        )
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO scope(key, label, kind) VALUES ('a','a','dir')"
            )

    def test_readers_and_writers_return_named_rows(self):
        writer = self.rw()
        insert_scope(writer, "/named")
        row = self.ro().execute("SELECT key, kind FROM scope").fetchone()
        self.assertEqual((row["key"], row["kind"]), ("/named", "git"))


class HotJournalTests(StoreCase):
    def leave_hot_journal(self):
        committed = self.rw()
        insert_scope(committed, "/committed")
        committed.close()
        child = Child(self, SPILLING_WRITER, self.db)
        child.wait_ready()
        child.kill()  # SIGKILL: the journal stays behind
        self.assertTrue(Path(f"{self.db}-journal").exists())

    def test_connect_ro_hot_journal_raises_hotjournal(self):
        self.leave_hot_journal()
        with self.assertRaises(store.HotJournalError) as caught:
            store.connect_ro(self.db)
        self.assertIsInstance(caught.exception, store.StoreUnavailableError)

    def test_heal_hot_journal_rolls_back_uncommitted_writes(self):
        self.leave_hot_journal()
        self.assertTrue(store.heal_hot_journal(self.db, self.home))
        self.assertFalse(Path(f"{self.db}-journal").exists())
        keys = [r["key"] for r in self.ro().execute("SELECT key FROM scope")]
        self.assertEqual(keys, ["/committed"])
        quick = self.ro().execute("PRAGMA quick_check").fetchone()[0]
        self.assertEqual(quick, "ok")

    def test_heal_hot_journal_false_while_writer_lock_is_held(self):
        self.leave_hot_journal()
        with store.writer_lock(self.home):
            self.assertFalse(store.heal_hot_journal(self.db, self.home))
        self.assertTrue(Path(f"{self.db}-journal").exists())
        with self.assertRaises(store.HotJournalError):
            store.connect_ro(self.db)
        self.assertTrue(store.heal_hot_journal(self.db, self.home))

    def test_heal_hot_journal_missing_db_returns_false(self):
        store.ensure_private_dir(self.home)
        self.assertFalse(store.heal_hot_journal(self.db, self.home))
        self.assertFalse(self.db.exists())


class WriterLockTests(StoreCase):
    def test_writer_lock_busy(self):
        holder = Child(self, HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        with self.assertRaises(store.BusyError):
            with store.writer_lock(self.home, wait_s=0):
                self.fail("lock was granted while another process held it")
        holder.kill()  # SIGKILL releases the flock: no stale lock
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_writer_lock_times_out_after_wait_s(self):
        holder = Child(self, HOLD_LOCK, self.home, 60)
        holder.wait_ready()
        start = time.monotonic()
        with self.assertRaises(store.BusyError):
            with store.writer_lock(self.home, wait_s=0.3):
                pass
        elapsed = time.monotonic() - start
        self.assertTrue(0.25 <= elapsed < 5, elapsed)

    def test_writer_lock_waits_for_release_then_acquires(self):
        holder = Child(self, HOLD_LOCK, self.home, 0.6)
        holder.wait_ready()
        start = time.monotonic()
        with store.writer_lock(self.home, wait_s=15):
            waited = time.monotonic() - start
        self.assertGreater(waited, 0.2)
        self.assertEqual(holder.proc.wait(timeout=10), 0)

    def test_writer_lock_is_not_reentrant_in_process(self):
        with store.writer_lock(self.home, wait_s=0):
            with self.assertRaises(store.BusyError):
                with store.writer_lock(self.home, wait_s=0):
                    pass


if __name__ == "__main__":
    unittest.main()


class IngestSchemaTests(StoreCase):
    """DDL additions for ingest: resumable parse state and O9 usage."""

    def test_source_parse_state_column(self):
        conn = self.rw()
        col = {r["name"]: r for r in conn.execute("PRAGMA table_info(source)")}
        state = col["parse_state"]
        self.assertEqual((state["type"], state["notnull"]), ("TEXT", 0))
        self.assertIsNone(state["dflt_value"])
        src = insert_source(conn)
        row = conn.execute(
            "SELECT parse_state FROM source WHERE id = ?", (src,)
        )
        self.assertIsNone(row.fetchone()["parse_state"])

    def test_usage_table_counts_per_source_and_cascades(self):
        conn = self.rw()
        cols = [
            (r["name"], r["type"], r["notnull"], r["pk"])
            for r in conn.execute("PRAGMA table_info(usage)")
        ]
        self.assertEqual(
            cols,
            [
                ("source_id", "INTEGER", 0, 1),
                ("provider", "TEXT", 1, 0),
                ("session_root", "TEXT", 1, 0),
                ("calls", "INTEGER", 1, 0),
                ("errors", "INTEGER", 1, 0),
                ("last_ts", "TEXT", 0, 0),
            ],
        )
        src, other = insert_source(conn, "t1"), insert_source(conn, "t2")
        conn.execute(
            "INSERT INTO usage(source_id, provider, session_root)"
            " VALUES (?, 'codex', 't1')",
            (src,),
        )
        row = conn.execute("SELECT calls, errors, last_ts FROM usage")
        self.assertEqual(tuple(row.fetchone()), (0, 0, None))
        with self.assertRaises(sqlite3.IntegrityError):  # provider CHECK
            conn.execute(
                "INSERT INTO usage(source_id, provider, session_root)"
                " VALUES (?, 'gpt', 't2')",
                (other,),
            )
        with self.assertRaises(sqlite3.IntegrityError):  # needs a source
            conn.execute(
                "INSERT INTO usage(source_id, provider, session_root)"
                " VALUES (999, 'codex', 'x')"
            )
        conn.execute("DELETE FROM source WHERE id = ?", (src,))
        left = conn.execute("SELECT count(*) FROM usage").fetchone()[0]
        self.assertEqual(left, 0)  # erasing a source row drops its counts
