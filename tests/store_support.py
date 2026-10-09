"""Shared fixtures for the store test modules.

Every helper works on temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import contextlib
import queue
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Generator
from pathlib import Path

from muninn import platform_io, store
from tests import test_configedit_windows

ROOT = Path(__file__).resolve().parent.parent

# Child scripts: argv[1] is the repo root, the rest are test arguments.
HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from muninn import store
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
from muninn import store
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


def mode(path: str | Path) -> int:
    """Return the permission bits of a path.

    Args:
        path: File or directory to inspect.

    Returns:
        The mode with the file-type bits masked off.
    """
    return stat.S_IMODE(Path(path).stat().st_mode)


def assert_private(
    case: unittest.TestCase, path: str | Path, *, directory: bool = False
) -> None:
    """Assert actual private access and exact POSIX permission bits.

    Args:
        case: Test case recording an assertion failure.
        path: Synthetic state object to inspect.
        directory: Whether the object must be a private directory.
    """
    value = Path(path)
    case.assertTrue(platform_io.is_private(value, directory=directory))
    if sys.platform != "win32":
        case.assertEqual(mode(value), 0o700 if directory else 0o600)


@contextlib.contextmanager
def public_read(
    case: unittest.TestCase, path: Path, *, directory: bool = False
) -> Generator[None]:
    """Temporarily grant public reads on a synthetic private state object.

    Args:
        case: Test case verifying the real unsafe and restored controls.
        path: Synthetic state object; never a user's state path.
        directory: Whether the object is a directory.

    Yields:
        Control while actual privacy validation refuses the object.
    """
    assert_private(case, path, directory=directory)
    original_mode = mode(path)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"],
                capture_output=True,
                check=True,
            )
        else:
            path.chmod(0o755 if directory else 0o644)
        case.assertFalse(platform_io.is_private(path, directory=directory))
        original_acl = (
            test_configedit_windows.descriptor(path)
            if sys.platform == "win32"
            else None
        )
        yield
        case.assertFalse(platform_io.is_private(path, directory=directory))
        if sys.platform == "win32":
            case.assertTrue(
                test_configedit_windows.descriptor(path) == original_acl,
                "unsafe state security descriptor changed",
            )
    finally:
        if sys.platform == "win32":
            subprocess.run(
                ["icacls.exe", str(path), "/remove:g", "*S-1-1-0"],
                capture_output=True,
                check=True,
            )
        else:
            path.chmod(original_mode)
    assert_private(case, path, directory=directory)


def _rowid(cursor: sqlite3.Cursor) -> int:
    """Return the row id of the insert that produced a cursor.

    Args:
        cursor: Cursor returned by an INSERT statement.

    Returns:
        The id of the inserted row.
    """
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_scope(
    conn: sqlite3.Connection, key: str = "/repo", kind: str = "git"
) -> int:
    """Insert a scope row labelled after the last path segment.

    Args:
        conn: Open writer connection.
        key: Scope key, usually a path.
        kind: Scope kind accepted by the schema CHECK.

    Returns:
        The new scope id.
    """
    label = key.rsplit("/", 1)[-1] or key
    return _rowid(
        conn.execute(
            "INSERT INTO scope(key, label, kind) VALUES (?, ?, ?)",
            (key, label, kind),
        )
    )


def insert_source(
    conn: sqlite3.Connection,
    thread_id: str = "t1",
    thread_class: str = "primary",
) -> int:
    """Insert a minimal codex source row.

    Args:
        conn: Open writer connection.
        thread_id: Thread id, also used as the session root and file name.
        thread_class: Thread class accepted by the schema CHECK.

    Returns:
        The new source id.
    """
    return _rowid(
        conn.execute(
            "INSERT INTO source(provider, thread_id, session_root,"
            " thread_class, class_reason, replay_mode, root, path,"
            " first_line_sha256, ino, size, mtime_ns, classifier_version,"
            " first_seen, last_seen)"
            " VALUES ('codex', ?, ?, ?, 'test', 'none', 'codex-sessions', ?,"
            " 'h', 1, 1, 1, 1, 0, 0)",
            (thread_id, thread_id, thread_class, f"{thread_id}.jsonl"),
        )
    )


def insert_event(
    conn: sqlite3.Connection,
    source_id: int,
    scope_id: int,
    **kw: str | int | None,
) -> int:
    """Insert an event row, defaulting every column a test does not set.

    Args:
        conn: Open writer connection.
        source_id: Owning source row.
        scope_id: Owning scope row.
        **kw: Overrides for line, part, role, kind, cwd, parent or text.

    Returns:
        The new event id.
    """
    row = {
        "line": 1,
        "part": 1,
        "role": "user",
        "kind": "prompt",
        "cwd": None,
        "parent": None,
        "text": "hello",
    } | kw
    return _rowid(
        conn.execute(
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
        )
    )


class Child:
    """A python subprocess that prints 'ready' once it holds its resource.

    Attributes:
        proc: The running child process with piped stdout and stderr.
    """

    def __init__(
        self, case: unittest.TestCase, code: str, *args: object
    ) -> None:
        """Start the child and register its cleanup on the test case.

        Args:
            case: Test case whose cleanup list kills the child.
            code: Python source run with ``-c``; argv[1] is the repo root.
            *args: Extra arguments, stringified, passed after the repo root.
        """
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT)]
            + [str(a) for a in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        case.addCleanup(self.kill)

    def wait_ready(self, timeout: float = 30) -> None:
        """Block until the child prints ``ready``.

        Args:
            timeout: Seconds to wait for the line.

        Raises:
            AssertionError: If the child exits or stays silent.
        """
        stdout, stderr = self.proc.stdout, self.proc.stderr
        assert stdout is not None and stderr is not None
        received: queue.Queue[str] = queue.Queue()

        def read() -> None:
            received.put(stdout.readline())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            line = received.get(timeout=timeout)
        except queue.Empty:
            line = ""
        if line.strip() != "ready" and self.proc.poll() is None:
            self.proc.kill()
        reader.join(timeout=5)
        assert not reader.is_alive(), "child readiness reader did not stop"

        if line.strip() != "ready":
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            errors = stderr.read()  # before kill() closes the pipe
            self.kill()
            raise AssertionError(f"child not ready: {errors}")

    def kill(self) -> None:
        """Kill the child if it still runs and close its pipes."""
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()


class StoreCase(unittest.TestCase):
    """Base case with a private temp home and connection helpers.

    Attributes:
        tmp: Temp directory removed after the test.
        home: Data dir inside ``tmp``; created lazily by the store.
        db: Path of the sqlite file under ``home``.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.db = store.db_path(self.home)

    def rw(self, *, fullfsync: bool = True) -> sqlite3.Connection:
        """Open a writer connection that closes at test cleanup.

        Args:
            fullfsync: Whether the writer requests full fsync.

        Returns:
            The open read-write connection.
        """
        conn = store.connect_rw(self.db, fullfsync=fullfsync)
        self.addCleanup(conn.close)
        return conn

    def ro(self) -> sqlite3.Connection:
        """Open a reader connection that closes at test cleanup.

        Returns:
            The open read-only connection.
        """
        conn = store.connect_ro(self.db)
        self.addCleanup(conn.close)
        return conn

    def residue(self, needles: list[bytes]) -> int:
        """Count needle occurrences in every file under the data dir.

        Args:
            needles: Byte strings to search for.

        Returns:
            Total occurrences across all files.
        """
        return sum(
            p.read_bytes().count(n)
            for p in self.home.rglob("*")
            if p.is_file()
            for n in needles
        )
