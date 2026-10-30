"""Exercise host approval through the actual temporary installer."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Callable
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from install import host_renewal, host_renewal_receipts
from install.context import Ctx
from install.host_renewal_models import HostPlan, RenewalReceipt
from install.host_renewal_preview import preview
from install.record import Record, load_record
from muninn import review_recovery, store
from tests.host_renewal_support import HostFixture
from tools.codex_policy_artifacts import ArtifactPlan


class ApplyFixture(HostFixture):
    """Run the actual entry with temporary external boundaries."""

    def setUp(self) -> None:
        super().setUp()
        lock = self.ctx.lib / ".install-record.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        self.enterContext(
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                return_value=self.current_host,
            )
        )

    def interrupt_at(self, point: str) -> HostPlan:
        """Interrupt only after the actual requested receipt is durable."""
        plan = preview(self.ctx, self.repo, self.sha)
        original = host_renewal_receipts.phase

        def crash(
            ctx: Ctx, rec: Record, name: str, **fields: object
        ) -> RenewalReceipt:
            value = original(ctx, rec, name, **fields)
            if name == point:
                raise KeyboardInterrupt("synthetic durable interruption")
            return value

        with (
            mock.patch.object(host_renewal, "phase", crash),
            mock.patch.object(host_renewal_receipts, "phase", crash),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_renewal(plan)
        return plan


class HostRenewalApplyTests(ApplyFixture):
    """Preserve saved facts while granting exactly one host."""

    def test_renewal_preserves_snapshots_and_activation_time(self) -> None:
        for mode in ("now", "all"):
            with self.subTest(mode=mode):
                if mode == "all":
                    self.doCleanups()
                    self.setUp()
                self.seed_inventory()
                self.sql("UPDATE learning_setup SET mode=?", (mode,))
                plan = preview(self.ctx, self.repo, self.sha)
                before, snapshots = (
                    self.read_control(),
                    self.request_snapshots(),
                )
                with mock.patch(
                    "install.learning_scope.apply",
                    side_effect=AssertionError("scope changed"),
                ):
                    result = self.run_renewal(plan)
                current = self.read_control()
                self.assertEqual(
                    current["policy_digest"], plan.candidate["digest"]
                )
                self.assertEqual(
                    current["activated_at"], before["activated_at"]
                )
                self.assertEqual(
                    current["generation"],
                    result["host_renewal"]["predecessor"]["generation"] + 1,
                )
                self.assertEqual(self.request_snapshots(), snapshots)
                self.assertEqual(
                    result["host_renewal"]["phase"], "upgrade_verified"
                )

    def test_new_work_before_commit_invalidates_plan(self) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        self.request("failed")
        before = self.read_control()
        with self.assertRaisesRegex(
            AssertionError, "actual renewal entry refused"
        ):
            self.run_renewal(plan)
        self.assertEqual(self.read_control(), before)

    def test_return_to_old_digest_refuses_terminal_rows(self) -> None:
        for state in (
            "completed",
            "failed",
            "cancelled",
            "waiting",
            "pending",
            "running",
        ):
            with self.subTest(state=state):
                self.sql("DELETE FROM review_request")
                self.request(state, candidate=True)
                with self.assertRaisesRegex(
                    Exception, "host_renewal_digest_reused"
                ):
                    preview(self.ctx, self.repo, self.sha)

    def test_verify_failure_retains_committed_grant(self) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        self.world.fake.doctor = 1
        with self.assertRaisesRegex(
            AssertionError, "actual renewal entry refused"
        ):
            self.run_renewal(plan)
        receipt = load_record(self.ctx.rdir / "rollback-record.json")[
            "host_renewal"
        ]
        self.assertEqual(receipt["phase"], "restart_pending")
        self.assertEqual(
            self.read_control()["policy_digest"], plan.candidate["digest"]
        )
        self.assertIsNone(self.world.fake.loaded)
        self.world.fake.doctor = 0
        generation = self.read_control()["generation"]
        result = self.run_renewal(plan)
        self.assertEqual(result["host_renewal"]["phase"], "upgrade_verified")
        self.assertEqual(self.read_control()["generation"], generation)

    def test_commit_pending_recovers_actual_commit_once(self) -> None:

        plan = preview(self.ctx, self.repo, self.sha)
        original = host_renewal.phase

        def crash(
            ctx: Ctx, rec: Record, name: str, **fields: object
        ) -> RenewalReceipt:
            if name == "grant_committed":
                raise KeyboardInterrupt("synthetic crash after commit")
            return original(ctx, rec, name, **fields)

        with (
            mock.patch.object(host_renewal, "phase", crash),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_renewal(plan)
        receipt = load_record(self.ctx.rdir / "rollback-record.json")[
            "host_renewal"
        ]
        self.assertEqual(receipt["phase"], "commit_pending")
        generation = self.read_control()["generation"]
        result = self.run_renewal(plan)
        self.assertEqual(result["host_renewal"]["phase"], "upgrade_verified")
        self.assertEqual(self.read_control()["generation"], generation)

    def test_verified_paused_renewal_does_not_report_old_host_notice(
        self,
    ) -> None:
        self.sql("UPDATE review_control SET admission=0")
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            review_recovery.record(
                conn, "setup", "codex_host_changed", "launch", 1.0
            )
            conn.execute("COMMIT")
        plan = preview(self.ctx, self.repo, self.sha)
        self.run_renewal(plan)
        with closing(store.connect_ro(self.path)) as conn:
            action = review_recovery.startup_action(conn)
            assert action is not None
            self.assertEqual(action["code"], "paused")
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                0,
            )

    def test_no_call_attempts_end_truthfully_without_changing_snapshots(
        self,
    ) -> None:
        request = self.request()
        attempt = self.attempt(request)
        self.sql("UPDATE review_control SET generation=1")
        plan = preview(self.ctx, self.repo, self.sha)
        snapshots = self.request_snapshots()
        with mock.patch("install.cutover.time.time", self.ctx.now):
            result = self.run_renewal(plan)
        self.assertEqual(self.request_snapshots(), snapshots)
        with closing(store.connect_ro(self.path)) as conn:
            ended = dict(
                conn.execute(
                    "SELECT * FROM review_attempt WHERE id=?", (attempt,)
                ).fetchone()
            )
            current = dict(
                conn.execute(
                    "SELECT * FROM review_request WHERE id=?", (request,)
                ).fetchone()
            )
            self.assertEqual(ended["outcome"], "abandoned")
            self.assertEqual(ended["ended_at"], ended["progress_at"])
            self.assertEqual(current["state"], "failed")
            self.assertIsNone(current["current_attempt_id"])
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
        self.assertEqual(result["host_renewal"]["phase"], "upgrade_verified")

    def test_new_work_immediately_before_commit_refuses_without_grant(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        before = self.read_control()
        original = host_renewal.publish_artifacts

        def changed(home: Path, recipe: ArtifactPlan) -> None:
            original(home, recipe)
            self.request("failed", candidate=True)

        with (
            mock.patch.object(host_renewal, "publish_artifacts", changed),
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        self.assertEqual(
            self.read_control()["policy_digest"], before["policy_digest"]
        )
        self.assertNotEqual(
            self.read_control()["generation"], before["generation"]
        )

    def test_each_durable_phase_crash_retains_exact_authority(self) -> None:
        for point in (
            "approved_intent",
            "contained_archived",
            "commit_pending",
            "grant_committed",
            "restart_pending",
        ):
            with self.subTest(phase=point):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)
                original = host_renewal.phase

                def crash(
                    ctx: Ctx,
                    rec: Record,
                    name: str,
                    original: Callable[..., RenewalReceipt] = original,
                    point: str = point,
                    **fields: object,
                ) -> RenewalReceipt:
                    value = original(ctx, rec, name, **fields)
                    if name == point:
                        raise KeyboardInterrupt(
                            "synthetic durable phase crash"
                        )
                    return value

                with (
                    mock.patch.object(host_renewal, "phase", crash),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    self.run_renewal(plan)
                saved = load_record(self.ctx.rdir / "rollback-record.json")[
                    "host_renewal"
                ]
                self.assertEqual(saved["plan"]["digest"], plan.digest)
                self.assertEqual(saved["phase"], point)
                if point == "approved_intent":
                    with self.assertRaisesRegex(
                        AssertionError, "actual renewal entry refused"
                    ):
                        self.run_renewal(plan)
                else:
                    result = self.run_renewal(plan)
                    self.assertEqual(
                        result["host_renewal"]["phase"], "upgrade_verified"
                    )

    def test_archive_failure_retains_partial_evidence_without_grant(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        before = self.read_control()["policy_digest"]
        with (
            mock.patch.object(
                host_renewal.shutil,
                "copyfileobj",
                side_effect=OSError("synthetic archive copy failure"),
            ),
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        saved = load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(saved["host_archive"]["status"], "partial")
        self.assertEqual(saved["host_renewal"]["phase"], "approved_intent")
        self.assertTrue(Path(saved["host_archive"]["path"]).exists())
        self.assertEqual(self.read_control()["policy_digest"], before)

    def test_changed_files_after_pending_refuse_before_commit(self) -> None:
        for target in (
            "config",
            "service",
            "candidate",
            "runtime",
            "artifact",
            "cwd",
            "operation",
        ):
            with self.subTest(target=target):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)
                original = host_renewal.phase

                def change(
                    ctx: Ctx,
                    rec: Record,
                    name: str,
                    original: Callable[..., RenewalReceipt] = original,
                    target: str = target,
                    **fields: object,
                ) -> RenewalReceipt:
                    result = original(ctx, rec, name, **fields)
                    if name == "commit_pending":
                        paths = {
                            "config": ctx.config,
                            "service": ctx.plist,
                            "candidate": ctx.review_policy,
                            "runtime": ctx.lib
                            / rec["sha"]
                            / "muninn/review_codex_output.py",
                            "artifact": ctx.data / "review-codex-schema.json",
                            "cwd": ctx.data / "review-runner-cwd/changed",
                            "operation": ctx.lib / ".install-record.lock",
                        }
                        path = paths[target]
                        assert path is not None
                        path.write_bytes(b"synthetic drift")
                    return result

                with (
                    mock.patch.object(host_renewal, "phase", change),
                    self.assertRaisesRegex(
                        AssertionError, "actual renewal entry refused"
                    ),
                ):
                    self.run_renewal(plan)
                self.assertEqual(
                    self.read_control()["policy_digest"], plan.old["digest"]
                )

    def test_tombstone_replay_runs_only_outside_policy_transaction(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        original = host_renewal.tombstones.reapply_tombstones
        calls: list[bool] = []

        def replay(conn: sqlite3.Connection, home: Path) -> None:
            calls.append(conn.in_transaction)
            original(conn, home)

        with mock.patch.object(
            host_renewal.tombstones, "reapply_tombstones", replay
        ):
            self.run_renewal(plan)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {False})

    def test_conflicting_candidate_artifact_returns_finite_refusal(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        path = (
            self.ctx.data
            / "review-capabilities/selected"
            / (plan.candidate["digest"] + ".json")
        )
        raw = b"synthetic private artifact conflict"
        path.write_bytes(raw)
        path.chmod(0o600)
        with (
            redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        self.assertNotIn(raw.decode(), output.getvalue())
        self.assertIn("FAILED: host_renewal_", output.getvalue())
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(
            self.read_control()["policy_digest"], plan.old["digest"]
        )
        self.assertIsNone(self.world.fake.loaded)
