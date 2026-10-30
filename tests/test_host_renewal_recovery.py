"""Check exact pending grant recognition independently of current fencing."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import partial
from typing import Any
from unittest import mock

from install import (
    cutover,
    finish,
    host_renewal,
    host_renewal_receipts,
    lifecycle,
    policy_records,
)
from install.context import Ctx, StepFailedError
from install.host_renewal_models import (
    CommitPending,
    HostState,
    RenewalReceipt,
)
from install.host_renewal_preview import preview
from install.host_renewal_recovery import recognize
from install.record import Record, load_record
from muninn import file_sync, review_recovery, store
from muninn import review_pause as q
from muninn.review_codex_launch import fingerprint
from tests.host_renewal_record_support import ReceiptFixture, artifact_inputs
from tests.test_host_renewal_apply import ApplyFixture
from tools.codex_policy_artifacts import plan_artifacts, publish_artifacts


class HostRenewalRecoveryTests(ReceiptFixture):
    """A committed successor must never become a second grant."""

    def test_commit_without_receipt_is_recognized_once(self) -> None:

        receipt = policy_records.host_renewal(self.phase("commit_pending"))
        assert isinstance(receipt, CommitPending)
        contained = receipt.contained
        successor = HostState.model_validate_json(
            contained.model_copy(
                update={"control": receipt.successor}
            ).model_dump_json()
        )
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                recognize(conn, receipt, contained), "predecessor"
            )
            self.assertEqual(recognize(conn, receipt, successor), "successor")
            self.assertEqual(recognize(conn, receipt, successor), "successor")

    def test_receipt_requires_actual_held_lock(self) -> None:
        value = self.phase("commit_pending")
        for field in (
            "held_operation_lock",
            "bindings",
            "setup_notice",
            "prior_summary",
            "recognized_summary",
        ):
            broken = dict(value)
            broken.pop(field)
            with self.subTest(field=field), self.assertRaises(StepFailedError):
                policy_records.host_renewal(broken)
        value["bindings"]["installation"]["expected_selection"]["current"] = (
            "f" * 40
        )
        with self.assertRaises(StepFailedError):
            policy_records.host_renewal(value)


class HostRenewalNoticeTests(ApplyFixture):
    """Retire only the exact approved stale setup row after a durable grant."""

    def seed_notice(self, code: str) -> None:
        """Save a real setup diagnostic through the normal writer API."""
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            review_recovery.record(conn, "setup", code, "launch", 1.0)
            conn.execute("COMMIT")

    def test_legacy_notice_retires_with_old_projectless_request(self) -> None:
        for admission in (0, 1):
            with self.subTest(admission=admission):
                self.doCleanups()
                self.setUp()
                self.sql("UPDATE review_control SET admission=?", (admission,))
                old_request = self.request("failed")
                self.sql(
                    "UPDATE review_request SET scope_json=?",
                    ('{"project_id":null,"task":null}',),
                )
                self.seed_notice("codex_experimental_boundary")
                snapshots = self.request_snapshots()
                result = self.run_renewal(
                    preview(self.ctx, self.repo, self.sha)
                )
                self.assertEqual(self.request_snapshots(), snapshots)
                notice = result["host_renewal"]["setup_notice"]
                self.assertEqual(notice["code"], "codex_experimental_boundary")
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertIsNone(
                        conn.execute(
                            "SELECT * FROM review_issue WHERE id='setup'"
                        ).fetchone()
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT state FROM review_request WHERE id=?",
                            (old_request,),
                        ).fetchone()[0],
                        "failed",
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM review_receipt"
                        ).fetchone()[0],
                        0,
                    )
                    action = review_recovery.startup_action(conn)
                    if admission == 0:
                        assert action is not None
                        self.assertEqual(action["code"], "paused")
                    else:
                        self.assertTrue(
                            action is None
                            or action["code"] != "codex_experimental_boundary"
                        )
                self.assertEqual(self.read_control()["admission"], admission)

    def test_changed_or_new_candidate_notice_is_retained(self) -> None:
        for change in ("candidate", "row"):
            with self.subTest(change=change):
                self.doCleanups()
                self.setUp()
                self.seed_notice("codex_experimental_boundary")
                original = cutover.clear_host_notice

                def changed(
                    ctx: Ctx,
                    owner: q.MaintenanceOwner,
                    receipt: Record,
                    original: Callable[
                        [Ctx, q.MaintenanceOwner, Record], None
                    ] = original,
                    change: str = change,
                ) -> None:
                    if change == "candidate":
                        self.request("failed", candidate=True, number=2)
                    else:
                        self.sql(
                            "UPDATE review_issue SET created_at=2.0 "
                            "WHERE id='setup'"
                        )
                    original(ctx, owner, receipt)

                with mock.patch.object(cutover, "clear_host_notice", changed):
                    self.run_renewal(preview(self.ctx, self.repo, self.sha))
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertIsNotNone(
                        conn.execute(
                            "SELECT * FROM review_issue WHERE id='setup'"
                        ).fetchone()
                    )

    def test_cleanup_crashes_resume_historical_grant_without_refencing(
        self,
    ) -> None:
        for after in (False, True):
            with self.subTest(after=after):
                self.doCleanups()
                self.setUp()
                self.seed_notice("codex_experimental_boundary")
                plan = preview(self.ctx, self.repo, self.sha)
                original = cutover.clear_host_notice

                def crash(
                    ctx: Ctx,
                    owner: q.MaintenanceOwner,
                    receipt: Record,
                    original: Callable[
                        [Ctx, q.MaintenanceOwner, Record], None
                    ] = original,
                    after: bool = after,
                ) -> None:
                    if after:
                        original(ctx, owner, receipt)
                    raise KeyboardInterrupt("synthetic setup cleanup crash")

                with (
                    mock.patch.object(cutover, "clear_host_notice", crash),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    self.run_renewal(plan)
                generation = self.read_control()["generation"]
                self.run_renewal(plan)
                self.assertEqual(self.read_control()["generation"], generation)
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertIsNone(
                        conn.execute(
                            "SELECT * FROM review_issue WHERE id='setup'"
                        ).fetchone()
                    )


class HostArtifactPublicationTests(ReceiptFixture):
    """Publish the exact finite recipe and retain fault evidence."""

    def test_invalid_recipe_refuses_before_publication(self) -> None:
        binding, prompt = artifact_inputs(self)
        recipe = plan_artifacts(
            self.candidate, prompt, binding, fingerprint(binding)
        )
        changed = dataclasses.replace(recipe, digest="0" * 64)
        with self.assertRaisesRegex(
            ValueError, "preparation_artifact_invalid"
        ):
            publish_artifacts(self.ctx.data, changed)
        selected = (
            self.ctx.data
            / "review-capabilities/selected"
            / (recipe.policy_digest + ".json")
        )
        self.assertFalse(selected.exists())

    def test_partial_publication_is_retained_and_exact_retry_is_idempotent(
        self,
    ) -> None:
        binding, prompt = artifact_inputs(self)
        recipe = plan_artifacts(
            self.candidate, prompt, binding, fingerprint(binding)
        )
        original = file_sync.sync_fd

        def failed(fd: int) -> None:
            original(fd)
            raise OSError("synthetic durable sync failure")

        with (
            mock.patch.object(file_sync, "sync_fd", failed),
            self.assertRaises(OSError),
        ):
            publish_artifacts(self.ctx.data, recipe)
        self.assertTrue(
            any(
                (self.ctx.data / item.relative_path).exists()
                for item in recipe.files
            )
        )
        publish_artifacts(self.ctx.data, recipe)
        before = {
            str(item.relative_path): (self.ctx.data / item.relative_path)
            .stat()
            .st_ino
            for item in recipe.files
        }
        publish_artifacts(self.ctx.data, recipe)
        self.assertEqual(
            before,
            {
                str(item.relative_path): (self.ctx.data / item.relative_path)
                .stat()
                .st_ino
                for item in recipe.files
            },
        )
        for item in recipe.files:
            self.assertEqual(
                (self.ctx.data / item.relative_path).read_bytes(), item.raw
            )


class HostRenewalCommitFailureTests(ApplyFixture):
    """Observe both sides of a real SQLite commit before recovery."""

    def test_database_commit_error_retains_actual_grant_state(self) -> None:
        for after in (False, True):
            with self.subTest(after_commit=after):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)

                class CommitFailure(sqlite3.Connection):
                    """Fail only the policy-grant transaction at COMMIT."""

                    complete = after
                    armed = False

                    def execute(
                        self,
                        sql: str,
                        parameters: Any = (),
                    ) -> sqlite3.Cursor:
                        """Fail before or after the real grant COMMIT."""
                        if sql.startswith(
                            "UPDATE review_control SET policy_json="
                        ):
                            self.armed = True
                        if sql == "COMMIT" and self.armed:
                            self.armed = False
                            if self.complete:
                                super().execute(sql, parameters)
                            raise sqlite3.OperationalError(
                                "synthetic commit response"
                            )
                        return super().execute(sql, parameters)

                with (
                    mock.patch.object(
                        sqlite3,
                        "connect",
                        partial(sqlite3.connect, factory=CommitFailure),
                    ),
                    self.assertRaisesRegex(
                        AssertionError, "actual renewal entry refused"
                    ),
                ):
                    self.run_renewal(plan)
                saved = load_record(self.ctx.rdir / "rollback-record.json")[
                    "host_renewal"
                ]
                self.assertEqual(
                    saved["phase"],
                    "grant_committed" if after else "commit_pending",
                )
                current = self.read_control()
                self.assertEqual(
                    current["policy_digest"],
                    plan.candidate["digest"] if after else plan.old["digest"],
                )
                result = self.run_renewal(plan)
                self.assertEqual(
                    result["host_renewal"]["phase"], "upgrade_verified"
                )
                self.assertEqual(
                    self.read_control()["generation"],
                    current["generation"] + (0 if after else 1),
                )

    def test_external_candidate_row_after_pending_refuses(self) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        original = host_renewal.phase

        def change(
            ctx: Ctx, rec: dict[str, object], name: str, **fields: object
        ) -> RenewalReceipt:
            value = original(ctx, rec, name, **fields)
            if name == "commit_pending":
                self.request("completed", candidate=True)
            return value

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
        with self.assertRaisesRegex(
            AssertionError, "actual renewal entry refused"
        ):
            self.run_renewal(plan)

    def test_verified_receipt_finishes_without_restarting_or_regranting(
        self,
    ) -> None:
        for point in ("verified", "finish"):
            with self.subTest(point=point):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)
                phase, save = host_renewal_receipts.phase, finish.save

                def crash_phase(
                    ctx: Ctx,
                    rec: dict[str, object],
                    name: str,
                    phase: Callable[..., RenewalReceipt] = phase,
                    point: str = point,
                    **fields: object,
                ) -> RenewalReceipt:
                    value = phase(ctx, rec, name, **fields)
                    if name == "upgrade_verified" and point == "verified":
                        raise KeyboardInterrupt("synthetic verified crash")
                    return value

                def crash_finish(
                    ctx: Ctx,
                    rec: dict[str, object],
                    save: Callable[..., None] = save,
                ) -> None:
                    if rec.get("install_complete") is True:
                        raise OSError("synthetic finish publication")
                    save(ctx, rec)

                with (
                    mock.patch.object(
                        host_renewal_receipts, "phase", crash_phase
                    ),
                    mock.patch.object(finish, "save", crash_finish),
                    self.assertRaises((KeyboardInterrupt, AssertionError)),
                ):
                    self.run_renewal(plan)
                saved = load_record(self.ctx.rdir / "rollback-record.json")
                self.assertEqual(
                    saved["host_renewal"]["phase"], "upgrade_verified"
                )
                self.assertIsNot(saved.get("install_complete"), True)
                control = self.read_control()
                with (
                    mock.patch.object(
                        lifecycle, "stop", side_effect=AssertionError("stop")
                    ),
                    mock.patch.object(
                        host_renewal,
                        "activate",
                        side_effect=AssertionError("grant"),
                    ),
                ):
                    result = self.run_renewal(plan)
                self.assertTrue(result["install_complete"])
                self.assertEqual(self.read_control(), control)
