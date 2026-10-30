"""Check historical grants before resumed effects and final publication."""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Callable
from contextlib import ExitStack, closing
from functools import partial
from unittest import mock

from install import (
    cutover,
    finish,
    host_renewal,
    host_renewal_receipts,
    host_renewal_resume,
    lifecycle,
)
from install.context import Ctx
from install.host_renewal_models import PendingFacts
from install.host_renewal_preview import preview
from install.host_renewal_recovery import pending
from install.record import Record, load_record
from muninn import store
from muninn.review_progress import digest
from tests.test_host_renewal_apply import ApplyFixture


class HostRenewalResumeTests(ApplyFixture):
    """Preserve grant truth while rejecting changed recovery authority."""

    def grow_verified(
        self, ctx: Ctx, rec: Record, point: str, verified: Callable[..., None]
    ) -> None:
        """Inject oversized final metadata after the actual durable receipt."""
        verified(ctx, rec)
        if point == "envelope":
            rec["sweep_failed"] = ["x" * (4 * 1024 * 1024)]

    def acknowledge(
        self, ctx: Ctx, rec: Record, point: str, save: Callable[..., None]
    ) -> None:
        """Fail immediately before or after the completion write."""
        if rec.get("install_complete") is True and point == "ack_before":
            raise OSError("synthetic completion failure")
        save(ctx, rec)
        if rec.get("install_complete") is True and point == "ack_after":
            raise OSError("synthetic completion lost response")

    def test_each_final_publication_boundary_is_idempotently_repairable(
        self,
    ) -> None:
        for point in (
            "summary_before",
            "summary_after",
            "ack_before",
            "ack_after",
            "envelope",
        ):
            with self.subTest(point=point):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)
                summary, save = finish.install_record, finish.save
                verified = host_renewal_receipts.verified

                def publish(
                    ctx: Ctx,
                    rec: Record,
                    outcome: str,
                    point: str = point,
                    summary: Callable[..., None] = summary,
                ) -> None:
                    self.assertNotEqual(point, "envelope")
                    if point == "summary_before":
                        raise OSError("synthetic summary failure")
                    summary(ctx, rec, outcome)
                    if point == "summary_after":
                        raise OSError("synthetic summary lost response")

                with (
                    mock.patch.object(
                        host_renewal_receipts,
                        "verified",
                        partial(
                            self.grow_verified, point=point, verified=verified
                        ),
                    ),
                    mock.patch.object(finish, "install_record", publish),
                    mock.patch.object(
                        finish,
                        "save",
                        partial(self.acknowledge, point=point, save=save),
                    ),
                    self.assertRaisesRegex(
                        AssertionError, "actual renewal entry refused"
                    ),
                ):
                    self.run_renewal(plan)
                control = self.read_control()
                self.assertEqual(
                    control["policy_digest"], plan.candidate["digest"]
                )
                with ExitStack() as guards:
                    for module, name in (
                        (lifecycle, "stop"),
                        (lifecycle, "start"),
                        (host_renewal, "activate"),
                        (cutover, "clear_host_notice"),
                        (finish, "sweep"),
                        (finish, "discard"),
                        (finish, "supersede"),
                    ):
                        guards.enter_context(
                            mock.patch.object(
                                module, name, side_effect=AssertionError(name)
                            )
                        )
                    result = self.run_renewal(plan)
                    self.assertTrue(result["install_complete"])
                    with (
                        mock.patch.object(
                            finish,
                            "install_record",
                            side_effect=AssertionError("rewrote summary"),
                        ),
                        mock.patch.object(
                            finish,
                            "save",
                            side_effect=AssertionError("rewrote completion"),
                        ),
                    ):
                        self.run_renewal(plan)
                self.assertEqual(self.read_control(), control)
                for selection in (None, "preview", "f" * 64):
                    self.assertIsNone(
                        pending(
                            dataclasses.replace(
                                self.ctx, renew_codex_host=selection
                            )
                        )
                    )

    def test_all_postgrant_phases_refuse_changed_immutable_authority(
        self,
    ) -> None:
        for phase in (
            "grant_committed",
            "restart_pending",
            "upgrade_verified",
        ):
            for change in (
                "control",
                "store",
                "artifact",
                "host",
                "config",
                "runtime",
            ):
                with self.subTest(phase=phase, change=change):
                    self.doCleanups()
                    self.setUp()
                    plan = self.interrupt_at(phase)
                    if change == "control":
                        self.sql(
                            "UPDATE review_control SET activated_at=999.0"
                        )
                    elif change == "store":
                        replacement = self.path.with_suffix(".replacement")
                        shutil.copy2(self.path, replacement)
                        replacement.replace(self.path)
                    elif change == "artifact":
                        selected = (
                            self.ctx.data
                            / "review-capabilities/selected"
                            / (plan.candidate["digest"] + ".json")
                        )
                        selected.write_bytes(
                            b"synthetic conflicting capability"
                        )
                    elif change in {"config", "runtime"}:
                        target = (
                            self.ctx.config
                            if change == "config"
                            else self.ctx.lib
                            / self.sha
                            / "muninn/review_codex_output.py"
                        )
                        target.write_bytes(
                            target.read_bytes() + b"\n# synthetic drift\n"
                        )
                    host = "f" * 64 if change == "host" else self.current_host
                    control = self.read_control()
                    with (
                        mock.patch(
                            "muninn.review_codex_policy.host_identity",
                            return_value=host,
                        ),
                        mock.patch(
                            "muninn.review_codex_launch.host_identity",
                            return_value=host,
                        ),
                        self.assertRaisesRegex(
                            AssertionError, "actual renewal entry refused"
                        ),
                    ):
                        self.run_renewal(plan)
                    self.assertEqual(self.read_control(), control)
                    saved = load_record(self.ctx.rdir / "rollback-record.json")
                    self.assertEqual(saved["host_renewal"]["phase"], phase)
                    self.assertIsNot(saved.get("install_complete"), True)

    def test_postrestart_work_charges_receipts_and_progress_remain_allowed(
        self,
    ) -> None:
        plan = self.interrupt_at("upgrade_verified")
        request = self.request("completed", candidate=True)
        attempt = self.attempt(request)
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
            "outcome='completed'"
        )
        self.sql(
            "UPDATE review_request SET state='completed',"
            "current_attempt_id=NULL"
        )
        self.insert(
            "review_source",
            {
                "provider": "codex",
                "thread_id": "fixture",
                "activation_line": 0,
                "examined_line": 4,
                "eligible_line": 4,
                "completed_line": 4,
                "updated_at": 4.0,
            },
        )
        self.insert(
            "review_call",
            {
                "attempt_id": attempt,
                "round_index": 1,
                "budget_account_id": "synthetic-account",
                "policy_digest": digest(self.candidate),
                "state": "completed",
                "reserved_at": 2.0,
                "requested_json": "{}",
                "observed_json": "{}",
                "cost_reservation_decimal": "0.03",
                "currency": "USD",
            },
        )
        self.insert(
            "review_receipt",
            {
                "request_id": request,
                "attempt_id": attempt,
                "applied_json": "[]",
                "unresolved_json": "[]",
                "committed_at": 3.0,
                "outcome": "no_change",
            },
        )
        tables = (
            "review_request",
            "review_attempt",
            "review_call",
            "review_receipt",
            "review_source",
        )
        with closing(store.connect_ro(self.path)) as conn:
            before = {
                name: tuple(
                    tuple(row) for row in conn.execute("SELECT * FROM " + name)
                )
                for name in tables
            }
        self.run_renewal(plan)
        with closing(store.connect_ro(self.path)) as conn:
            after = {
                name: tuple(
                    tuple(row) for row in conn.execute("SELECT * FROM " + name)
                )
                for name in tables
            }
        self.assertEqual(after, before)

    def test_host_change_between_capture_and_guard_returns_finite_refusal(
        self,
    ) -> None:
        plan = self.interrupt_at("upgrade_verified")
        capture = host_renewal_resume.capture

        def drift(
            ctx: Ctx, rec: Record, receipt: PendingFacts
        ) -> host_renewal_resume.ResumeInputs:
            result = capture(ctx, rec, receipt)
            self.enterContext(
                mock.patch(
                    "muninn.review_codex_policy.host_identity",
                    return_value="f" * 64,
                )
            )
            return result

        control = self.read_control()
        with (
            mock.patch.object(host_renewal_resume, "capture", drift),
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        self.assertEqual(self.read_control(), control)
