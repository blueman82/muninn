"""Exercise erasure through real ownership and disposable stores."""

from __future__ import annotations

import os
from unittest import mock

from muninn import (
    erase,
    poller_stop,
    review_commit,
    review_requests,
    store,
)
from muninn import review_pause_state as state
from muninn.review_pause import EraseStatus
from tests.ingest_support import TID, IngestCase
from tests.review_pause_support import initialize
from tests.test_review_commit import CommitCase

MARKER = "ERASUREPROCESSPRIVATECANARY"


class ErasePauseTests(IngestCase):
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
