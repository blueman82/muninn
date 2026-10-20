"""Preserve uncertainty and current erasure intent across repair failures."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from muninn import cli_serve, store, tombstone_key, tombstones
from muninn import review_quiescence as quiescence
from muninn import review_quiescence_state as state
from tests.review_quiescence_support import interrupt_erasure
from tests.store_support import Child
from tests.test_review_quiescence import BOOT_A, BOOT_B, QuiescenceCase


class QuiescenceRepairTests(QuiescenceCase):
    """Only completed recovery and latest-intent replay permit completion."""

    def test_ordinary_entry_replays_interrupted_intent_before_yield(
        self,
    ) -> None:
        before = interrupt_erasure(self.home, "fixture-thread")
        with (
            mock.patch.object(
                quiescence.review_boot_identity,
                "observe",
                side_effect=AssertionError("no reboot needed"),
            ),
            quiescence.erase_connection(
                self.home, wait_s=0, status=self.status
            ) as conn,
        ):
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
            )
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)

    def test_ingestion_owner_replays_before_active(self) -> None:
        interrupt_erasure(self.home, "fixture-thread")
        poller = cli_serve._Poller(self.home, {}, 60)
        self.addCleanup(poller.close)
        poller._start_review()
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
            )

    def test_replay_failure_never_yields_or_retires_then_retries(self) -> None:
        before = interrupt_erasure(self.home, "fixture-thread")
        baseline = self.home / state.BASELINE
        store.write_json_atomic(baseline, {"obsolete": True})
        retained = baseline.read_bytes()
        replay = quiescence.reapply_tombstones
        original = OSError("replay cleanup")

        def after_commit(conn: sqlite3.Connection, home: Path) -> int:
            replay(conn, home)
            raise original

        for failure in (original, after_commit):
            with (
                mock.patch.object(
                    quiescence, "reapply_tombstones", side_effect=failure
                ),
                self.assertRaises(OSError) as caught,
                quiescence.erase_connection(
                    self.home, wait_s=1, status=self.status
                ),
            ):
                self.fail("failed replay yielded")
            self.assertIs(caught.exception, original)
            self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
            self.assertEqual(baseline.read_bytes(), retained)
        if os.name == "nt":
            with (
                self.assertRaisesRegex(ValueError, "quiescence_unverified"),
                quiescence.erase_connection(
                    self.home, wait_s=1, status=self.status
                ),
            ):
                self.fail("Windows retired an unverified baseline")
            self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
            self.assertEqual(baseline.read_bytes(), retained)
            return
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe"
            ) as boot,
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.assertFalse(baseline.exists())
        boot.assert_not_called()

    def generation(self) -> int:
        """Read the durable generation without a writer root."""
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            return conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]

    def test_baseline_visibility_failure_never_certifies_repair(self) -> None:
        self.abandon()
        original = state.publish

        def visible(
            home: Path, fd: int, name: str, value: state.StrictModel
        ) -> None:
            original(home, fd, name, value)
            raise OSError("sync after publication")

        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_A
            ),
            mock.patch.object(state, "publish", side_effect=visible),
            self.assertRaises(OSError),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("incomplete baseline admitted")
        self.assertEqual(self.status.repair_phase, "none")
        self.assertTrue((self.home / state.BASELINE).exists())
        self.refuse()

    def test_reconciliation_failure_rolls_back_and_keeps_uncertain_witness(
        self,
    ) -> None:
        before = self.abandon()
        self.refuse()
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(
                quiescence,
                "recover_attempts",
                side_effect=ValueError("recovery"),
            ),
            self.assertRaisesRegex(ValueError, "recovery"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("failed recovery admitted")
        self.assertEqual(self.generation(), 0)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertEqual(self.status.repair_phase, "reconciling")

    def test_replay_failure_preserves_committed_generation_and_baseline(
        self,
    ) -> None:
        before = self.abandon()
        self.refuse()
        baseline = (self.home / state.BASELINE).read_bytes()
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(
                quiescence, "reapply_tombstones", side_effect=OSError("replay")
            ),
            self.assertRaisesRegex(OSError, "replay"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("failed replay admitted")
        self.assertEqual(self.generation(), 1)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertEqual((self.home / state.BASELINE).read_bytes(), baseline)

    def test_complete_publication_failure_leaves_obsolete_baseline(
        self,
    ) -> None:
        self.abandon()
        self.refuse()
        original = state.publish

        def visible(
            home: Path, fd: int, name: str, value: state.StrictModel
        ) -> None:
            original(home, fd, name, value)
            raise OSError("after complete visibility")

        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(state, "publish", side_effect=visible),
            self.assertRaises(OSError),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("publication failure admitted")
        self.assertEqual(self.status.repair_phase, "reconciling")
        self.assertTrue((self.home / state.BASELINE).exists())
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError, "quiescence_unverified"):
                state.begin_owner(self.home)
            self.assertTrue((self.home / state.BASELINE).exists())
            return
        fd, token = state.begin_owner(self.home)
        self.addCleanup(os.close, fd)
        self.assertFalse((self.home / state.BASELINE).exists())
        state.complete_owner(self.home, fd, token)

    def test_retirement_failure_cannot_claim_repair_complete(self) -> None:
        self.abandon()
        self.refuse()
        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(
                state, "retire_baseline", side_effect=OSError("retire")
            ),
            self.assertRaisesRegex(OSError, "retire"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("failed retirement admitted")
        self.assertEqual(self.status.repair_phase, "reconciling")
        self.assertEqual(
            json.loads((self.home / state.WITNESS).read_bytes())["state"],
            "complete",
        )
        self.assertTrue((self.home / state.BASELINE).exists())

    def test_changed_baseline_during_replay_is_not_overwritten(self) -> None:
        before = self.abandon()
        self.refuse()
        path = self.home / state.BASELINE
        changed = b'{"changed":true}'

        def drift(conn: sqlite3.Connection, home: Path) -> int:
            path.write_bytes(changed)
            return 0

        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(
                quiescence, "reapply_tombstones", side_effect=drift
            ),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.fail("changed authority admitted")
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual((self.home / state.WITNESS).read_bytes(), before)

    def test_replaced_lock_and_corrupt_baseline_require_new_baseline(
        self,
    ) -> None:
        self.abandon()
        self.refuse()
        (self.home / state.LOCK).rename(self.home / "old-lock")
        self.refuse(BOOT_B)
        value = json.loads((self.home / state.BASELINE).read_bytes())
        self.assertEqual(value["boot"], BOOT_B)
        self.assertEqual(
            value["lock_identity"][1], (self.home / state.LOCK).stat().st_ino
        )
        (self.home / state.BASELINE).write_bytes(b"corrupt")
        self.refuse(BOOT_B)

    @unittest.skipIf(os.name == "nt", "Windows refuses baseline retirement")
    def test_latest_intent_replay_preserves_key_current_store_and_archive(
        self,
    ) -> None:
        key = tombstone_key.load_key(self.home)
        archive = self.home / "muninn.sqlite.pre-upgrade-fixture"
        archive.write_bytes(b"retained archive fixture")
        before = self.abandon()
        self.refuse()
        with store.writer_lock(self.home, wait_s=0):
            tombstones.append_tombstones(
                self.home,
                [
                    tombstones.tombstone_row(
                        "codex", "thread", thread_id="fixture-thread"
                    )
                ],
            )
            with contextlib.closing(
                store.connect_rw(store.db_path(self.home))
            ) as conn:
                conn.execute("CREATE TABLE wanted(value TEXT)")
                conn.execute("INSERT INTO wanted VALUES('keep current data')")
        intent = (self.home / tombstones.TOMBSTONE_FILE).read_bytes()
        inode = store.db_path(self.home).stat().st_ino
        replay = quiescence.reapply_tombstones

        def checked(conn: sqlite3.Connection, home: Path) -> int:
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                1,
            )
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(home, wait_s=0),
            ):
                self.fail("writer released before replay")
            return replay(conn, home)

        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(
                quiescence, "reapply_tombstones", side_effect=checked
            ),
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ) as conn,
        ):
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT value FROM wanted").fetchone()[0],
                "keep current data",
            )
        self.assertNotEqual((self.home / state.WITNESS).read_bytes(), before)
        self.assertEqual(tombstone_key.load_key(self.home), key)
        self.assertEqual(
            (self.home / tombstones.TOMBSTONE_FILE).read_bytes(), intent
        )
        self.assertEqual(archive.read_bytes(), b"retained archive fixture")
        self.assertEqual(store.db_path(self.home).stat().st_ino, inode)

    @unittest.skipIf(os.name == "nt", "Windows refuses baseline retirement")
    def test_original_consumer_error_wins_over_close_failure(self) -> None:
        self.abandon()
        self.refuse()
        connect = store.connect_rw
        original = LookupError("consumer")

        def failing_close(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            proxy = mock.Mock(wraps=conn)
            type(proxy).in_transaction = mock.PropertyMock(
                side_effect=lambda: conn.in_transaction
            )

            def close() -> None:
                conn.close()
                raise OSError("close")

            proxy.close.side_effect = close
            return cast(sqlite3.Connection, proxy)

        with (
            mock.patch.object(
                quiescence.review_boot_identity, "observe", return_value=BOOT_B
            ),
            mock.patch.object(store, "connect_rw", side_effect=failing_close),
            self.assertRaises(LookupError) as caught,
            quiescence.erase_connection(
                self.home, wait_s=1, status=self.status
            ),
        ):
            self.status.erasure_phase = "intent_durable"
            raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(self.status.erasure_phase, "intent_durable")
        self.assertEqual(self.status.repair_phase, "complete")
        fd = state.acquire(self.home)
        os.close(fd)

    def test_separate_ingestion_owner_consumes_exact_stop_then_completes(
        self,
    ) -> None:
        child = Child(
            self,
            """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from muninn import cli_serve
home = Path(sys.argv[2])
env = {"HOME": str(home / "unused"), "MUNINN_ROOTS": json.dumps({})}
poller = cli_serve._Poller(home, env, 60)
poller._start_review()
(home / "fixture-child.pid").write_text(str(os.getpid()))
print("ready", flush=True)
deadline = time.monotonic() + 10
while not poller._requested():
    if time.monotonic() >= deadline:
        raise RuntimeError("stop not received")
    time.sleep(0.01)
poller.close()
""",
            self.home,
        )
        child.wait_ready(timeout=10)
        with quiescence.erase_connection(
            self.home, wait_s=5, status=self.status
        ):
            witness = state.parse(
                state.inspect(self.home, state.WITNESS)[0], state.Witness
            )
            assert witness is not None and witness.owner is not None
            self.assertEqual(
                witness.owner.pid,
                int((self.home / "fixture-child.pid").read_text()),
            )
            self.assertEqual(witness.state, "complete")
        out, err = child.proc.communicate(timeout=5)
        self.assertEqual((child.proc.returncode, out, err), (0, "", ""))
        self.assertTrue(self.status.stop_may_have_been_published)
