"""Exercise erasure through real ownership and disposable stores."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import (
    erase,
    platform_windows,
    poller_stop,
    review_commit,
    review_requests,
    store,
)
from muninn import review_quiescence_state as state
from muninn.review_quiescence import EraseStatus
from tests.ingest_support import TID, IngestCase
from tests.review_quiescence_support import initialize
from tests.test_review_commit import CommitCase
from tests.test_review_supervisor import SupervisorCase

MARKER = "ERASUREPROCESSPRIVATECANARY"


def wait_file(path: Path) -> None:
    """Bound disposable fixture synchronization without touching services."""
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not path.exists():
        raise AssertionError(f"fixture did not publish {path.name}")


def owner_fixture(control: Path, replacement: bool) -> None:
    """Run the real owner and monitor around a local Python wrapper fixture."""
    fixture = SupervisorCase()
    try:
        try:
            fixture.setUp()
        except PermissionError as error:
            if sys.platform == "win32" and hasattr(fixture, "tmp"):
                for ancestor in reversed((fixture.tmp, *fixture.tmp.parents)):
                    try:
                        platform_windows.assert_ancestry(ancestor)
                    except PermissionError:
                        raise PermissionError(
                            f"{error}; rejected ancestor: {ancestor}"
                        ) from error
            raise
        fixture.script = (
            "import os,time; from pathlib import Path; "
            f"Path({str(control / 'cli-started')!r})"
            ".write_text(str(os.getpid())); "
            f"print({MARKER!r},flush=True); time.sleep(30)"
        )
        supervisor = fixture.supervisor
        deadline = time.monotonic() + 8
        while (
            not (control / "cli-started").exists()
            and time.monotonic() < deadline
        ):
            supervisor.tick(13.0)
            time.sleep(0.01)
        wait_file(control / "cli-started")
        pending = next(iter(supervisor._rounds.values()))
        assert pending.handle is not None
        wrapper = pending.handle.wrapper
        work = fixture.home / "review-work" / pending.context.attempt_id
        work.mkdir(parents=True)
        (work / "prompt").write_text(MARKER)
        ready = {
            "home": str(fixture.home),
            "work": str(work),
            "provider": str(fixture.path),
            "token": supervisor.stop_generation,
            "cli_pid": int((control / "cli-started").read_text()),
        }
        del pending
        store.write_json_atomic(control / "ready.json", ready)
        assert supervisor.stop_generation is not None
        assert poller_stop.wait(
            fixture.home, os.getpid(), supervisor.stop_generation, 8
        )
        assert not (fixture.home / erase.TOMBSTONE_FILE).exists()
        assert (work / "prompt").read_text() == MARKER
        supervisor.stop(14.0)
        assert wrapper.poll() is not None
        assert not supervisor.monitor._thread.is_alive()
        assert not any(
            (
                supervisor.monitor.slots,
                supervisor._rounds,
                supervisor._pending,
                supervisor._next,
                supervisor.artifact,
                supervisor.capability,
            )
        )
        if replacement:
            fd, token = state.begin_owner(fixture.home)
            state.complete_owner(fixture.home, fd, token)
            os.close(fd)
        (control / "cleaned").write_text("contained and buffers discarded")
        wait_file(control / "release")
    finally:
        fixture.doCleanups()


class EraseQuiescenceTests(IngestCase):
    """Require containment before any requested erasure mutation."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)
        self.env = {"HOME": str(self.tmp), "MUNINN_ROOTS": "{}"}

    def test_absent_store_refuses_without_creation(self) -> None:
        absent = self.tmp / "absent"
        with self.assertRaisesRegex(
            store.StoreUnavailableError, "store is absent"
        ):
            erase.run_erase(absent, session="missing", env=self.env)
        self.assertFalse(absent.exists())

    def test_request_status_survives_original_append_failure(self) -> None:
        status = EraseStatus()
        original = OSError("append interrupted")
        with (
            mock.patch.object(
                erase, "append_tombstones", side_effect=original
            ),
            self.assertRaises(OSError) as caught,
        ):
            erase.run_erase_request(
                self.home,
                erase.EraseRequest(session="missing"),
                status=status,
                env=self.env,
            )
        self.assertIs(caught.exception, original)
        self.assertEqual(status.erasure_phase, "intent_may_be_durable")

    def test_compatibility_and_request_paths_erase_exactly_once(self) -> None:
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                before = self.conn.execute(
                    "SELECT count(*) FROM tombstone"
                ).fetchone()[0]
                if explicit:
                    status = EraseStatus()
                    out = erase.run_erase_request(
                        self.home,
                        erase.EraseRequest(session="missing"),
                        status=status,
                        wait_s=0,
                        env=self.env,
                    )
                    self.assertEqual(status.erasure_phase, "verified")
                else:
                    out = erase.run_erase(
                        self.home,
                        session="missing",
                        wait_s=0,
                        env=self.env,
                    )
                after = self.conn.execute(
                    "SELECT count(*) FROM tombstone"
                ).fetchone()[0]
                self.assertEqual(after - before, out["tombstones"])

    def test_low_level_wrapper_keeps_caller_owned_barriers(self) -> None:
        fd = state.acquire(self.home)
        self.addCleanup(os.close, fd)
        with store.writer_lock(self.home, wait_s=0):
            out = erase.erase(
                self.conn, home=self.home, session="missing", env=self.env
            )
        self.assertEqual(out["residue"], 0)

    def test_zero_wait_busy_owner_never_erases_or_publishes_stop(self) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(ValueError, "coordination_timeout"):
            erase.run_erase(
                self.home, session="missing", wait_s=0, env=self.env
            )
        self.assertFalse((self.home / poller_stop.FILE).exists())
        self.assertFalse((self.home / erase.TOMBSTONE_FILE).exists())

    def test_invalid_event_syntax_precedes_database_creation(self) -> None:
        absent = self.tmp / "invalid"
        with self.assertRaises(ValueError):
            erase.run_erase(absent, event_ref="bad", env=self.env)
        self.assertFalse(absent.exists())

    def test_dry_run_keeps_writer_only_path_and_empty_store_behavior(
        self,
    ) -> None:
        fd, _ = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        out = erase.run_erase(
            self.home, session="missing", dry_run=True, env=self.env
        )
        self.assertTrue(out["dry_run"])
        self.assertFalse((self.home / poller_stop.FILE).exists())
        absent = self.tmp / "dry"
        erase.run_erase(absent, session="missing", dry_run=True, env=self.env)
        self.assertTrue(store.db_path(absent).exists())
        self.assertFalse((absent / state.WITNESS).exists())

    def start_owner(
        self, replacement: bool = False
    ) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
        """Start an isolated service fixture with guaranteed cleanup."""
        control = self.tmp / "control"
        control.mkdir()
        code = (
            "from pathlib import Path; from tests.test_erase_quiescence "
            "import owner_fixture; "
            f"owner_fixture(Path({str(control)!r}), {replacement!r})"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parent.parent,
            env={
                "HOME": str(self.tmp),
                "USERPROFILE": str(self.tmp),
                "TMPDIR": str(self.tmp),
                "TEMP": str(self.tmp),
                "TMP": str(self.tmp),
                **{
                    key: os.environ[key]
                    for key in ("LD_LIBRARY_PATH", "SYSTEMROOT")
                    if key in os.environ
                },
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def close() -> None:
            (control / "release").touch()
            try:
                _, errors = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, errors = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, errors.decode())

        self.addCleanup(close)
        wait_file(control / "ready.json")
        return proc, json.loads((control / "ready.json").read_text())

    def test_selected_owner_discards_buffers_and_children_before_erase(
        self,
    ) -> None:
        proc, ready = self.start_owner()
        home, work = Path(ready["home"]), Path(ready["work"])
        original = Path(ready["provider"]).read_bytes()
        append = erase.append_tombstones
        status = EraseStatus()

        def after_cleanup(*args: Any) -> None:
            wait_file(self.tmp / "control" / "cleaned")
            with self.assertRaises(ProcessLookupError):
                os.kill(ready["cli_pid"], 0)
            self.assertEqual((work / "prompt").read_text(), MARKER)
            self.assertTrue(status.stop_may_have_been_published)
            append(*args)

        with mock.patch.object(
            erase, "append_tombstones", side_effect=after_cleanup
        ):
            out = erase.run_erase_request(
                home,
                erase.EraseRequest(session=TID),
                wait_s=5,
                status=status,
                env=self.env,
            )
        self.assertIsNone(proc.poll())
        self.assertFalse(work.exists())
        review = out["review"]
        assert isinstance(review, dict)
        self.assertEqual(review["working_files"], 1)
        self.assertEqual(status.erasure_phase, "verified")
        self.assertEqual(Path(ready["provider"]).read_bytes(), original)
        target = json.loads((home / poller_stop.FILE).read_text())
        self.assertEqual(
            target, {"pid": proc.pid, "generation": ready["token"]}
        )

    def test_replacement_owner_refuses_without_second_stop_or_intent(
        self,
    ) -> None:
        proc, ready = self.start_owner(replacement=True)
        home = Path(ready["home"])
        request = poller_stop.request

        def publish(*args: Any, **kwargs: Any) -> None:
            request(*args, **kwargs)
            wait_file(self.tmp / "control" / "cleaned")

        status = EraseStatus()
        with (
            mock.patch.object(poller_stop, "request", side_effect=publish),
            self.assertRaisesRegex(ValueError, "owner_changed"),
        ):
            erase.run_erase_request(
                home,
                erase.EraseRequest(session=TID),
                wait_s=5,
                status=status,
                env=self.env,
            )
        self.assertEqual(status.erasure_phase, "not_started")
        self.assertTrue(Path(ready["work"]).exists())
        self.assertFalse((home / erase.TOMBSTONE_FILE).exists())
        self.assertEqual(
            json.loads((home / poller_stop.FILE).read_text()),
            {"pid": proc.pid, "generation": ready["token"]},
        )


class InterruptedEraseTests(CommitCase):
    """Join real erase intent with foreground refusal and startup recovery."""

    def test_interrupted_erase_fences_new_writes_and_replays_before_startup(
        self,
    ) -> None:
        initialize(self.home)
        first = self.context
        receipt = self.save(
            self.result([]).model_copy(update={"no_change_reason": MARKER})
        )
        self.context = self.start(3, 3)
        work = self.home / "review-work" / self.context.attempt_id
        work.mkdir(parents=True)
        (work / "result").write_text(MARKER)
        original = self.path.read_bytes()
        status = EraseStatus()
        failure = OSError("after durable append")
        with (
            mock.patch.object(erase, "_commit", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            erase.run_erase_request(
                self.home,
                erase.EraseRequest(session=TID),
                status=status,
                env={"HOME": str(self.tmp), "MUNINN_ROOTS": "{}"},
                wait_s=0,
            )
        self.assertIs(caught.exception, failure)
        self.assertEqual(status.erasure_phase, "intent_durable")
        journal = (self.home / erase.TOMBSTONE_FILE).read_bytes()
        before = list(self.conn.iterdump())
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch(
                "muninn.review_originals.open_regular",
                side_effect=AssertionError("original read"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
                self.transaction(
                    lambda: review_requests.claim_proposal(
                        self.conn,
                        self.context.request_id,
                        1,
                        15.0,
                    )
                )
            with self.assertRaisesRegex(ValueError, "^erasure_pending$"):
                self.save(self.result([self.note()]))
            returned = review_commit.commit_review(
                self.conn, first, self.result([self.note()])
            )
        self.assertEqual(returned, receipt)
        self.assertEqual(list(self.conn.iterdump()), before)
        fd, token = state.begin_owner(self.home)
        try:
            self.assertFalse(work.exists())
            self.assertEqual(
                self.row(
                    "SELECT count(*) FROM review_request "
                    "WHERE state!='cancelled'"
                )[0],
                0,
            )
            self.assertEqual(
                self.row(
                    "SELECT count(*) FROM review_evidence "
                    "WHERE excerpt IS NOT NULL"
                )[0],
                0,
            )
            self.assertIsNone(
                self.row("SELECT no_change_reason FROM review_receipt")[0]
            )
        finally:
            state.complete_owner(self.home, fd, token)
            os.close(fd)
        with store.writer_lock(self.home, wait_s=0):
            with self.assertRaisesRegex(ValueError, "^rejected_fenced$"):
                self.save(self.result([self.note()]))
            returned = review_commit.commit_review(
                self.conn, first, self.result([])
            )
        self.assertEqual(
            returned, receipt.model_copy(update={"no_change_reason": None})
        )
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)
        self.assertEqual(
            (self.home / erase.TOMBSTONE_FILE).read_bytes(), journal
        )
        self.assertEqual(self.path.read_bytes(), original)
