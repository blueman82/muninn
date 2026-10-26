"""Check native Windows refusal without granting recovery authority."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import unittest
from collections.abc import Generator

from muninn import platform_io, store, tombstone_key, tombstones
from muninn import review_pause_state as state
from tests.erase_pause_ready import finish_owner, read_ready_text
from tests.lifecycle_native_windows import token_codes
from tests.review_pause_support import initialize, interrupt_erasure
from tests.store_support import (
    ROOT,
    StoreCase,
    direct_python,
    insert_event,
    insert_scope,
    insert_source,
    public_read,
)

TEXT = "disposable recovery preservation fixture"
OWNER = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from muninn import store
from muninn import review_pause_state as state
home, ready, release = map(Path, sys.argv[2:5])
fd, generation = state.begin_owner(home)
try:
    store.write_json_atomic(
        ready, {"pid": os.getpid(), "generation": generation}
    )
    deadline = time.monotonic() + 60
    while sys.argv[5] == "live" and not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture release timeout")
        time.sleep(0.01)
finally:
    os.close(fd)
"""
type Snapshot = dict[str, tuple[int, int, str]]


@unittest.skipUnless(sys.platform == "win32", "requires actual Windows APIs")
class WindowsRecoveryRefusalTests(StoreCase):
    """Compare populated native material around real refused CLI processes."""

    @classmethod
    def setUpClass(cls) -> None:
        """Record finite native token observations without desktop claims."""
        print(
            json.dumps(
                {
                    "probe": "windows_recovery_refusal_preservation",
                    "qualified": False,
                    "reboot_proof": False,
                    "desktop_proof": False,
                    "retirement_proof": False,
                    "token": token_codes(),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def setUp(self) -> None:
        super().setUp()
        platform_io.ensure_private_dir(self.home)
        initialize(self.home)
        with store.writer_lock(self.home, wait_s=0):
            tombstone_key.load_key(self.home)
            with contextlib.closing(store.connect_rw(self.db)) as conn:
                scope = insert_scope(conn)
                source = insert_source(conn, "fixture-thread")
                insert_event(conn, source, scope, text=TEXT)
        self.archive = self.home / "muninn.sqlite.pre-upgrade-fixture"
        with (
            contextlib.closing(store.connect_ro(self.db)) as source,
            contextlib.closing(sqlite3.connect(self.archive)) as target,
        ):
            source.backup(target)
        self.user = self.tmp / "isolated-user"
        self.environment = self.child_environment()
        self.assert_population(pending=False)

    def child_environment(self) -> dict[str, str]:
        """Retain OS loading paths while isolating every selected user root."""
        names = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "SYSTEMDRIVE",
            "COMSPEC",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in names
        }
        paths = {
            "HOME": self.user,
            "USERPROFILE": self.user,
            "APPDATA": self.user / "roaming",
            "LOCALAPPDATA": self.user / "local",
            "CODEX_HOME": self.user / "codex",
            "CLAUDE_CONFIG_DIR": self.user / "claude",
            "XDG_CONFIG_HOME": self.user / "config",
            "XDG_DATA_HOME": self.user / "data",
            "XDG_CACHE_HOME": self.user / "cache",
            "TEMP": self.user / "temp",
            "TMP": self.user / "temp",
        }
        for path in paths.values():
            platform_io.ensure_private_dir(path)
        environment |= {name: str(path) for name, path in paths.items()}
        return environment | {
            "MUNINN_HOME": str(self.home),
            "MUNINN_ROOTS": "{}",
            "MUNINN_CURSOR_DB": str(self.user / "absent-cursor.sqlite"),
        }

    def snapshot(self) -> Snapshot:
        """Read exact fixture bytes and native identities independently."""
        result: Snapshot = {}
        for path in self.home.rglob("*"):
            info = path.lstat()
            self.assertTrue(
                stat.S_ISREG(info.st_mode), "unexpected state object"
            )
            self.assertGreater(
                info.st_ino, 0, "native file identity unavailable"
            )
            relative = path.relative_to(self.home).as_posix()
            if relative == state.LOCK:
                # Windows byte-range locks can deny a read even past EOF.
                self.assertEqual(info.st_size, 0, "nonempty lifetime lock")
                data = b""
            else:
                data = path.read_bytes()
            result[relative] = (
                info.st_dev,
                info.st_ino,
                hashlib.sha256(data).hexdigest(),
            )
        return result

    def assert_population(self, *, pending: bool) -> None:
        """Require nonempty material and the expected durable intent."""
        self.assertEqual(
            (self.home / tombstone_key.KEY_FILE).stat().st_size, 32
        )
        for path in (self.db, self.archive):
            uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
            with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM source").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM event").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM tombstone").fetchone()[
                        0
                    ],
                    0,
                )
                if path == self.db:
                    if pending:
                        with self.assertRaisesRegex(
                            ValueError, "^erasure_pending$"
                        ):
                            tombstones.require_current_tombstones(
                                conn, self.home
                            )
                    else:
                        tombstones.require_current_tombstones(conn, self.home)
        if pending:
            intent = (self.home / tombstones.TOMBSTONE_FILE).read_bytes()
            self.assertGreater(len(intent.splitlines()), 0)

    def pending_intent(self) -> None:
        """Leave real thread and keyed intent unapplied."""
        interrupt_erasure(self.home, "fixture-thread")
        with store.writer_lock(self.home, wait_s=0):
            key = tombstone_key.load_key(self.home, create=False)
            tombstones.append_tombstones(
                self.home,
                [
                    tombstones.tombstone_row(
                        "codex",
                        "line",
                        thread_id="fixture-thread",
                        line=0,
                        line_sha256=tombstones.event_tag(key, "user", TEXT),
                    )
                ],
            )
        self.assert_population(pending=True)

    def recover(self, reason: str | None, *, pending: bool = False) -> None:
        """Check exact native results, file identities and fixture bytes."""
        before = self.snapshot()
        self.assertNotIn("calls.jsonl", before)
        executable, launcher = direct_python()
        result = subprocess.run(
            [executable, "-E", "-s", "-B", "-m", "muninn", "recover"],
            cwd=ROOT,
            env=self.environment | launcher,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.stderr, b"")
        value: object = json.loads(result.stdout)
        assert isinstance(value, dict)
        expected_exit = 2 if reason is not None else 0
        self.assertEqual(result.returncode, expected_exit)
        self.assertIs(value.get("logged"), True)
        if reason is not None:
            self.assertEqual(
                (value.get("error"), value.get("reason")), ("refused", reason)
            )
        else:
            recovery = value.get("recovery")
            assert isinstance(recovery, dict)
            self.assertEqual(recovery.get("status"), "no_recovery_needed")
        after = self.snapshot()
        self.assertEqual(set(after), set(before) | {"calls.jsonl"})
        after.pop("calls.jsonl")
        self.assertEqual(after, before)
        lines = (self.home / "calls.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        logged: object = json.loads(lines[0])
        assert isinstance(logged, dict)
        self.assertEqual(
            (logged.get("cmd"), logged.get("exit")), ("recover", expected_exit)
        )
        self.assert_population(pending=pending)

    @contextlib.contextmanager
    def owner(self, *, live: bool) -> Generator[subprocess.Popen[bytes]]:
        """Bound the owned process through readiness and cleanup."""
        ready, release = (
            self.tmp / "owner-ready.json",
            self.tmp / "owner-release",
        )
        executable, launcher = direct_python()
        proc = subprocess.Popen(
            [
                executable,
                "-I",
                "-B",
                "-c",
                OWNER,
                str(ROOT),
                str(self.home),
                str(ready),
                str(release),
                "live" if live else "exit",
            ],
            cwd=ROOT,
            env=self.environment | launcher,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started = False
        try:
            value: object = json.loads(read_ready_text(ready))
            assert isinstance(value, dict)
            self.assertEqual(value.get("pid"), proc.pid)
            witness = state.parse(
                state.inspect(self.home, state.WITNESS)[0], state.Witness
            )
            assert witness is not None and witness.owner is not None
            self.assertEqual(witness.state, "active")
            self.assertEqual(witness.owner.pid, proc.pid)
            self.assertEqual(witness.owner.generation, value.get("generation"))
            if not live:
                output, errors = proc.communicate(timeout=5)
                self.assertEqual(
                    (proc.returncode, output, errors), (0, b"", b"")
                )
            started = True
            yield proc
        finally:
            finish_owner(proc, release, started=started)

    def assert_owner_busy(self, proc: subprocess.Popen[bytes]) -> None:
        """Independently require a live child and held native lifetime lock."""
        self.assertIsNone(proc.poll())
        try:
            fd = state.acquire(self.home)
        except BlockingIOError:
            return
        os.close(fd)
        self.fail("live fixture lifetime lock became available")

    def test_healthy_complete_state_is_a_real_noop(self) -> None:
        witness = state.parse(
            state.inspect(self.home, state.WITNESS)[0], state.Witness
        )
        lock = state.inspect(self.home, state.LOCK)[1]
        assert witness is not None and lock is not None
        self.assertEqual(witness.state, "complete")
        self.assertEqual(witness.lock_identity, lock.identity)
        self.recover(None)

    def test_pending_keyed_intent_preserves_populated_material(self) -> None:
        self.pending_intent()
        self.recover("windows_repair_unqualified", pending=True)

    def test_abandoned_owner_and_synthetic_baseline_remain_unchanged(
        self,
    ) -> None:
        with self.owner(live=False):
            fd = state.acquire(self.home)
            try:
                with store.writer_lock(self.home, wait_s=0):
                    state.publish(
                        self.home,
                        fd,
                        state.BASELINE,
                        state.Baseline(
                            version=1,
                            lock_identity=state.lock_identity(self.home, fd),
                            boot="linux:12345678-1234-4234-8234-123456789abc",
                            predecessor=state.inspect(
                                self.home, state.WITNESS
                            )[1],
                        ),
                    )
            finally:
                os.close(fd)
            self.recover("windows_repair_unqualified")

    def test_live_predecessor_is_neither_stopped_nor_unlocked(self) -> None:
        with self.owner(live=True) as proc:
            self.assert_owner_busy(proc)
            self.recover("pause_unverified")
            self.assert_owner_busy(proc)

    def test_malformed_baseline_alias_preserves_the_actual_key(self) -> None:
        os.link(self.home / tombstone_key.KEY_FILE, self.home / state.BASELINE)
        key, alias = (self.home / tombstone_key.KEY_FILE).stat(), (
            self.home / state.BASELINE
        ).stat()
        self.assertEqual(
            (key.st_dev, key.st_ino), (alias.st_dev, alias.st_ino)
        )
        self.assertGreater(key.st_nlink, 1)
        self.recover("pause_unverified")

    def test_unsafe_native_witness_acl_refuses_without_mutation(self) -> None:
        with public_read(self, self.home / state.WITNESS):
            self.recover("pause_unverified")
