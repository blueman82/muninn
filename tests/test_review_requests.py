"""Check durable request admission and honest source progress."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import closing
from unittest import mock

from muninn import ingest, review_progress, review_requests, store
from muninn.knowledge_models import RequestId, Scope
from muninn.review_evidence_models import EvidenceWindow, SourceRef
from muninn.review_json import canonical_json
from muninn.review_models import RequestView, ReviewPolicy
from tests.ingest_support import TID, IngestCase, line, primary, rollout
from tests.test_classify import PARENT, codex_meta, reply, user_msg
from tests.test_review_shared_models import policy_data


class RequestCase(IngestCase):
    """Supply a real disposable indexed source and explicit review policy."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(
            os.environ,
            {
                "MUNINN_ROOTS": json.dumps(
                    {key: str(value) for key, value in self.roots.items()}
                )
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.records = primary()
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        data = policy_data()
        data.update(
            projection_version="permitted-fields-v2", max_input_bytes=4096
        )
        self.policy = ReviewPolicy.model_validate(data)
        key = self.conn.execute(
            "SELECT sc.key FROM scope sc JOIN event e ON sc.id=e.scope_id "
            "LIMIT 1"
        ).fetchone()[0]
        self.scope = Scope(project_id=key, task=None)
        self.conn.execute(
            "UPDATE review_control SET admission=1,generation=1,"
            "policy_json=?,policy_digest=?,activated_at=10.0",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def digest(self, policy: ReviewPolicy) -> str:
        """Hash explicit policy bytes for fixture control state."""
        return hashlib.sha256(canonical_json(policy).encode()).hexdigest()

    def transaction[T](self, action: Callable[[], T]) -> T:
        """Run the tested operation inside its caller-owned transaction."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            value = action()
            self.conn.execute("COMMIT")
            return value
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def window(self, first: int = 2, last: int = 3) -> EvidenceWindow:
        """Build a window from independently encoded complete originals."""
        return EvidenceWindow(
            source=SourceRef(provider="codex", thread_id=TID),
            from_line=first,
            through_line=last,
            boundary_hash=hashlib.sha256(
                line(self.records[last - 1]).rstrip(b"\r\n")
            ).hexdigest(),
        )

    def enqueue(self, window: EvidenceWindow | None = None) -> RequestView:
        """Admit a fixture interval through the public queue API."""
        return self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn,
                window or self.window(),
                self.scope,
                self.policy,
                "explicit",
                11.0,
            )
        )

    def receipt(self, request_id: RequestId) -> None:
        """Emulate a downstream atomic successful commit, without knowledge."""
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request_id, 1, 12.0
            )
        )
        self.conn.execute(
            "INSERT INTO review_receipt VALUES(?,?, '[]','[]',13.0,"
            "'no_change','No supported change')",
            (request_id, attempt.id),
        )
        self.conn.execute(
            "UPDATE review_request SET state='completed' WHERE id=?",
            (request_id,),
        )
        self.conn.execute(
            "UPDATE review_attempt SET ended_at=13.0,outcome='no_change' "
            "WHERE id=?",
            (attempt.id,),
        )


class ReviewRequestTests(RequestCase):
    """Catch duplicate work, premature coverage and resurrection."""

    def test_duplicate_window_policy_scope_is_one_request(self) -> None:
        first, second = self.enqueue(), self.enqueue()
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            1,
        )
        changed = self.policy.model_copy(update={"policy_id": "new"})
        other = self.transaction(
            lambda: review_requests.enqueue_review(
                self.conn, self.window(), self.scope, changed, "explicit", 12.0
            )
        )
        self.assertNotEqual(first.id, other.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_json FROM review_request WHERE id=?",
                (first.id,),
            ).fetchone()[0],
            canonical_json(self.policy),
        )

    def test_partial_line_not_eligible(self) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.append(self.path, [], line(user_msg(4, "Use partial"))[:-1])
        self.run_ingest()
        ids = self.transaction(
            lambda: review_requests.reconcile_sources(
                self.conn, self.policy, 12.0
            )
        )
        self.assertEqual(ids, ())
        self.assertEqual(
            self.conn.execute(
                "SELECT examined_line,eligible_line FROM review_source"
            ).fetchone()[:],
            (3, 3),
        )
        with self.assertRaises(ValueError):
            self.enqueue(self.window().model_copy(update={"through_line": 4}))

    def test_failed_window_does_not_advance_completed(self) -> None:
        self.conn.execute(
            "INSERT INTO review_source VALUES" "('codex',?,1,3,3,1,NULL,10.0)",
            (TID,),
        )
        first = self.enqueue(self.window(2, 2))
        second = self.enqueue(self.window(3, 3))
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(self.conn, first.id, 1, 12.0)
        )
        self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "config", 13.0
            )
        )
        self.receipt(second.id)
        self.transaction(
            lambda: review_progress.advance_completed(
                self.conn, self.window().source, 14.0
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT completed_line FROM review_source"
            ).fetchone()[0],
            1,
        )
        self.transaction(
            lambda: review_requests.retry_request(self.conn, first.id, 15.0)
        )
        self.receipt(first.id)
        self.transaction(
            lambda: review_progress.advance_completed(
                self.conn, self.window().source, 16.0
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT completed_line FROM review_source"
            ).fetchone()[0],
            3,
        )

    def test_restart_fences_attempt_not_request(self) -> None:
        request = self.enqueue()
        old = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        recovered = self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 14.0)
        )
        self.assertEqual(recovered, (request.id,))
        new = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 2, 16.0
            )
        )
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(new.request_id, request.id)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT ended_at FROM review_attempt WHERE id=?", (old.id,)
            ).fetchone()[0]
        )

    def test_cancelled_window_suppressed_until_explicit_retry(self) -> None:
        request = self.enqueue()
        old = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 13.0)
        )
        duplicate = self.enqueue()
        self.assertEqual(
            (duplicate.id, duplicate.state), (request.id, "cancelled")
        )
        self.conn.execute("UPDATE review_control SET generation=2")
        self.transaction(
            lambda: review_requests.recover_attempts(self.conn, 2, 14.0)
        )
        self.assertEqual(self.enqueue().state, "cancelled")
        retry = self.transaction(
            lambda: review_requests.retry_request(self.conn, request.id, 15.0)
        )
        new = self.transaction(
            lambda: review_requests.claim_attempt(self.conn, retry.id, 2, 16.0)
        )
        self.assertEqual(retry.id, request.id)
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(new.outcome, "operator_retry")

    def test_retry_same_id_rechecks_tombstones(self) -> None:
        request = self.enqueue()
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 13.0)
        )
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,14.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        with self.assertRaisesRegex(ValueError, "evidence_erased"):
            self.transaction(
                lambda: review_requests.retry_request(
                    self.conn, request.id, 15.0
                )
            )
        self.assertEqual(self.enqueue().state, "cancelled")

    def test_activation_does_not_enqueue_history(self) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.assertEqual(
            self.transaction(
                lambda: review_requests.reconcile_sources(
                    self.conn, self.policy, 11.0
                )
            ),
            (),
        )
        self.records.append(user_msg(4, "Use Python after activation."))
        self.append(self.path, [self.records[-1]])
        self.run_ingest()
        ids = self.transaction(
            lambda: review_requests.reconcile_sources(
                self.conn, self.policy, 12.0
            )
        )
        self.assertEqual(len(ids), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT from_line,through_line FROM review_request"
            ).fetchone()[:],
            (4, 4),
        )

    def test_receipt_is_idempotent_and_survives_cancellation(self) -> None:
        request = self.enqueue()
        self.receipt(request.id)
        cancelled = self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 15.0)
        )
        retried = self.transaction(
            lambda: review_requests.retry_request(self.conn, request.id, 16.0)
        )
        self.assertEqual(
            (cancelled.state, retried.state), ("completed", "completed")
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )

    def test_auth_fault_has_no_automatic_retry(self) -> None:
        request = self.enqueue()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        failed = self.transaction(
            lambda: review_requests.fail_attempt(
                self.conn, attempt.id, "auth", 13.0
            )
        )
        self.assertEqual((failed.state, failed.retry_at), ("failed", None))
        with self.assertRaises(ValueError):
            self.transaction(
                lambda: review_requests.claim_attempt(
                    self.conn, request.id, 1, 100.0
                )
            )

    def test_caller_transaction_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "transaction"):
            review_requests.enqueue_review(
                self.conn,
                self.window(),
                self.scope,
                self.policy,
                "explicit",
                11.0,
            )

    def test_ingest_callback_only_reports_committed_sources(self) -> None:
        received: list[tuple[int, int, bool]] = []

        def committed(source_id: int) -> None:
            with closing(store.connect_ro(store.db_path(self.home))) as reader:
                last = reader.execute(
                    "SELECT cursor_line FROM source WHERE id=?", (source_id,)
                ).fetchone()[0]
            received.append((source_id, last, self.conn.in_transaction))

        self.append(self.path, [reply(4, "a new reply")])
        ingest.ingest(self.conn, self.roots, on_committed=committed)
        self.assertEqual(received, [(self.need_source()["id"], 4, False)])
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_insert BEFORE INSERT ON "
            "event BEGIN SELECT RAISE(ABORT,'test'); END"
        )
        self.append(self.path, [reply(5, "rolled back")])
        stats = ingest.ingest(self.conn, self.roots, on_committed=committed)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(self.need_source()["cursor_line"], 4)

    def test_late_forks_are_discovered_after_parent_commit(self) -> None:
        self.write(
            rollout("old-fork"),
            [
                codex_meta("user", "old-fork", forked_from_id=PARENT),
                user_msg(1, "parent q"),
                user_msg(2, "fork q"),
            ],
        )
        self.run_ingest()
        self.assertEqual(
            self.need_source("old-fork")["replay_mode"], "unverified"
        )
        self.write(
            rollout(PARENT),
            [
                codex_meta("user", PARENT),
                user_msg(1, "parent q"),
            ],
        )
        received: list[int] = []
        ingest.ingest(self.conn, self.roots, on_committed=received.append)
        self.assertEqual(
            received,
            [
                self.need_source(PARENT)["id"],
                self.need_source("old-fork")["id"],
            ],
        )
        self.assertEqual(
            self.need_source("old-fork")["replay_mode"], "content_prefix"
        )
        self.assertEqual(self.events("old-fork"), [(3, 1, "prompt", "fork q")])

    def test_complete_nonevent_boundary_stays_valid_after_append(self) -> None:
        self.records.append({"type": "noop"})
        self.append(self.path, [self.records[-1]])
        self.run_ingest()
        request = self.enqueue(self.window(2, 4))
        self.append(self.path, [user_msg(5, "new record")])
        self.run_ingest()
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 12.0
            )
        )
        self.assertEqual(attempt.request_id, request.id)
