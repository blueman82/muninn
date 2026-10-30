"""Exercise preserved historical work through actual host-only renewal."""

from __future__ import annotations

import dataclasses
import json
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import installer
from install.context import StepFailedError
from install.host_renewal_preview import preview
from muninn import (
    hook_learn,
    ingest,
    review_catchup,
    review_requests,
    review_supervisor,
    store,
)
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from tests.host_renewal_inventory import IndexedSource, seed_indexed
from tests.ingest_support import TID, line
from tests.test_classify import reply, user_msg
from tests.test_host_renewal_apply import ApplyFixture


class HostRenewalPreservationTests(ApplyFixture):
    """Retain immutable snapshots and each request's historical retry rules."""

    def test_four_no_call_jobs_use_their_own_historical_limits(self) -> None:
        historical = ReviewPolicy.model_validate_json(
            json.dumps(
                self.old.model_dump(mode="json")
                | {
                    "policy_id": "muninn-codex-ongoing-production-v3",
                    "max_attempts": 3,
                    "retry_backoff_seconds": [7.0, 19.0],
                }
            )
        )
        requests, attempts = [], []
        for number in range(1, 5):
            request = self.request(
                number=number,
                selected=historical if number in (2, 3) else None,
            )
            if number == 3:
                previous = self.attempt(request, number=6)
                self.sql(
                    "UPDATE review_attempt SET outcome='abandoned',"
                    "ended_at=3.0 WHERE id=?",
                    (previous,),
                )
            attempt = self.attempt(request, number=number)
            requests.append(request)
            attempts.append(attempt)

        self.sql(
            "UPDATE review_request SET state='cancelled' WHERE id=?",
            (requests[3],),
        )
        failed = self.request("failed", number=5)
        self.sql("UPDATE review_control SET generation=1")
        snapshots = self.request_snapshots()
        plan = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(len(plan.containment.jobs), 4)
        with mock.patch("install.cutover.time.time", self.ctx.now):
            self.run_renewal(plan)
        self.assertEqual(self.request_snapshots(), snapshots)
        with closing(store.connect_ro(self.path)) as conn:
            for index, (request, attempt) in enumerate(
                zip(requests, attempts, strict=True)
            ):
                row = conn.execute(
                    "SELECT * FROM review_request WHERE id=?", (request,)
                ).fetchone()
                ended = conn.execute(
                    "SELECT * FROM review_attempt WHERE id=?", (attempt,)
                ).fetchone()
                self.assertEqual(ended["outcome"], "abandoned")
                self.assertEqual(ended["ended_at"], ended["progress_at"])
                self.assertEqual(
                    row["state"],
                    ("failed", "waiting", "waiting", "cancelled")[index],
                )
                if index in (1, 2):
                    self.assertEqual(
                        row["retry_at"],
                        ended["ended_at"] + (7.0 if index == 1 else 19.0),
                    )
                if index != 3:
                    self.assertIsNone(row["current_attempt_id"])
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM review_request WHERE id=?", (failed,)
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_call").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_receipt").fetchone()[
                    0
                ],
                0,
            )

    def test_old_window_stays_blocked_and_new_hook_work_can_be_claimed(
        self,
    ) -> None:
        source = seed_indexed(self)
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            old = review_requests.enqueue_review(
                conn, source.window(), source.scope, self.old, "explicit", 10.0
            )
            conn.execute(
                "UPDATE review_request SET state='failed' WHERE id=?",
                (old.id,),
            )
            conn.execute(
                "UPDATE review_source SET examined_line=3,eligible_line=3"
            )
            conn.execute("COMMIT")
        plan = preview(self.ctx, self.repo, self.sha)
        self.run_renewal(plan)
        supervisor = review_supervisor.Supervisor(
            self.ctx.data, self.candidate
        )
        supervisor.start(20.0)
        try:
            self.assertIsNone(supervisor._claim(21.0))
            with (
                store.writer_lock(self.ctx.data),
                closing(store.connect_rw(self.path)) as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
                review_catchup.catch_up(conn, self.candidate, 21.0, 0)
                conn.execute("COMMIT")
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM review_request"
                    ).fetchone()[0],
                    1,
                )
            self.capture_new_interval(source)
            current = supervisor._claim(22.0)
            assert current is not None
            self.assertEqual(current.context.policy, self.candidate)
            self.assertGreater(
                current.context.admitted_windows[0].from_line, 3
            )
            with (
                store.writer_lock(self.ctx.data),
                closing(store.connect_rw(self.path)) as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
                explicit = review_requests.enqueue_review(
                    conn,
                    source.window(),
                    source.scope,
                    self.candidate,
                    "explicit",
                    23.0,
                )
                conn.execute("COMMIT")
                self.assertNotEqual(explicit.id, old.id)
                self.assertEqual(explicit.window, old.window)
                self.assertEqual(
                    conn.execute(
                        "SELECT state FROM review_request WHERE id=?",
                        (old.id,),
                    ).fetchone()[0],
                    "failed",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM review_call"
                    ).fetchone()[0],
                    0,
                )
        finally:
            supervisor.stop(24.0)

    def test_fresh_second_host_requires_a_new_explicit_plan(self) -> None:
        first = preview(self.ctx, self.repo, self.sha)
        initial = self.run_renewal(first)
        original_archive = Path(initial["host_renewal"]["archive_path"])
        self.ctx = dataclasses.replace(self.ctx, ts="20261002T000000Z")
        self.old = self.candidate
        next_host = "4" * 64
        values = self.old.model_dump(mode="json")
        values["experimental_codex"]["host_identity"] = next_host
        self.candidate = ReviewPolicy.model_validate_json(json.dumps(values))
        self.write_candidate(self.candidate)
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value=next_host,
            ),
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                return_value=next_host,
            ),
        ):
            second = preview(self.ctx, self.repo, self.sha)
            self.assertNotEqual(first.digest, second.digest)
            self.run_renewal(second)
        self.assertTrue(original_archive.exists())
        self.assertEqual(
            self.read_control()["policy_digest"], digest(self.candidate)
        )

    def capture_new_interval(self, source: IndexedSource) -> None:
        """Index and capture a real new hook interval from temporary bytes."""
        source.records.extend(
            (
                user_msg(3, "new synthetic question"),
                reply(4, "new synthetic answer"),
            )
        )
        source.path.write_bytes(b"".join(map(line, source.records)))
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            ingest.ingest(conn, source.roots)
        captured = hook_learn.capture_boundary(
            self.ctx.data,
            "codex",
            "session-end",
            json.dumps(
                {
                    "session_id": TID,
                    "cwd": str(self.home / "project"),
                    "transcript_path": str(source.path),
                    "hook_event_name": "SessionEnd",
                    "reason": "other",
                }
            ).encode(),
            self.candidate,
        )
        self.assertTrue(captured.accepted, captured)

    def test_return_to_first_host_refuses_each_retained_request_state(
        self,
    ) -> None:
        self.request("failed")
        original_policy = self.old
        self.run_renewal(preview(self.ctx, self.repo, self.sha))
        self.ctx = dataclasses.replace(self.ctx, ts="20261004T000000Z")
        self.candidate = original_policy
        self.write_candidate(self.candidate)
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value=self.old_host,
            ),
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                return_value=self.old_host,
            ),
        ):
            for state in (
                "pending",
                "running",
                "waiting",
                "completed",
                "failed",
                "cancelled",
            ):
                with self.subTest(state=state):
                    self.sql("UPDATE review_request SET state=?", (state,))
                    before = self.read_control()
                    with self.assertRaisesRegex(
                        StepFailedError, "host_renewal_digest_reused"
                    ):
                        preview(self.ctx, self.repo, self.sha)
                    self.assertEqual(self.read_control(), before)

    def test_later_explicit_scope_change_keeps_ordinary_semantics(
        self,
    ) -> None:
        result = self.run_renewal(preview(self.ctx, self.repo, self.sha))
        archive = Path(result["host_renewal"]["archive_path"])
        historical_record = archive.parent / "rollback-record.json"
        ctx = dataclasses.replace(
            self.ctx, ts="20261005T000000Z", dry_run=False, review_policy=None
        )
        with (
            mock.patch.object(installer, "Ctx", return_value=ctx),
            mock.patch("builtins.input", return_value="yes"),
        ):
            code = installer.main(
                [
                    "--repo",
                    str(self.repo),
                    "--sha",
                    self.sha,
                    "--upgrade",
                    "--learning-scope",
                    "now",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(archive.exists())
        self.assertEqual(
            json.loads(historical_record.read_text())["host_renewal"]["phase"],
            "upgrade_verified",
        )
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT mode FROM learning_setup").fetchone()[0],
                "now",
            )
        self.assertEqual(
            self.read_control()["policy_digest"], digest(self.candidate)
        )
