"""Retain exact summary provenance across interrupted host renewal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import ExitStack
from unittest import mock

from install import (
    finish,
    host_renewal,
    host_renewal_receipts,
    lifecycle,
    steps_release,
)
from install.context import Ctx
from install.host_renewal_models import HostPlan, RenewalReceipt
from install.host_renewal_preview import preview
from install.record import Record, install_summary, load_record
from tests.test_host_renewal_apply import ApplyFixture


class HostRenewalSummaryTests(ApplyFixture):
    """Recognize only exact prior status before advancing installer state."""

    def test_unknown_or_foreign_summary_is_not_overwritten(self) -> None:
        for change in ("foreign", "extra", "malformed", "coerced", "failure"):
            with self.subTest(change=change):
                self.doCleanups()
                self.setUp()
                plan = self.interrupt_at("upgrade_verified")
                rec = load_record(self.ctx.rdir / "rollback-record.json")
                value = install_summary(self.ctx, rec, "ok")
                if change == "foreign":
                    value["ts"] = "20261101T000000Z"
                elif change == "extra":
                    value["unexpected"] = "synthetic private body"
                elif change == "coerced":
                    value["host_approval_committed"] = 1
                elif change == "failure":
                    value.update(
                        host_renewal_phase="restart_pending",
                        outcome="recovery_required",
                        recovery_required=True,
                        failed={"step": "verify", "error": ["private body"]},
                    )
                else:
                    value["host_approval_committed"] = "yes"
                path = self.ctx.lib / "install-record.json"
                raw = json.dumps(value).encode()
                path.write_bytes(raw)
                path.chmod(0o600)
                with self.assertRaisesRegex(
                    AssertionError, "actual renewal entry refused"
                ):
                    self.run_renewal(plan)
                self.assertEqual(path.read_bytes(), raw)

    def test_exact_staged_prior_summary_can_be_replaced(self) -> None:
        path = self.ctx.lib / "install-record.json"
        path.write_bytes(b'{"prior":"synthetic ordinary summary"}')
        path.chmod(0o600)
        plan = self.interrupt_at("upgrade_verified")
        self.run_renewal(plan)
        self.assertEqual(
            load_record(path)["host_renewal_plan_digest"], plan.digest
        )

    def test_stale_own_failure_summary_can_finish_verified_operation(
        self,
    ) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        self.world.fake.doctor = 1
        with self.assertRaisesRegex(
            AssertionError, "actual renewal entry refused"
        ):
            self.run_renewal(plan)
        path = self.ctx.lib / "install-record.json"
        prior = path.read_bytes()
        self.assertEqual(
            load_record(path)["host_renewal_phase"], "restart_pending"
        )
        self.assertEqual(load_record(path)["outcome"], "recovery_required")
        self.world.fake.doctor = 0
        with (
            mock.patch.object(
                finish,
                "install_record",
                side_effect=OSError("synthetic final summary failure"),
            ),
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        self.assertEqual(path.read_bytes(), prior)
        control = self.read_control()
        self.run_renewal(plan)
        self.assertEqual(self.read_control(), control)
        self.assertEqual(load_record(path)["outcome"], "ok")

    def test_early_and_later_failures_resume_the_same_operation(self) -> None:
        for point in ("pause", "pin", "verify"):
            with self.subTest(point=point):
                self.doCleanups()
                self.setUp()
                plan = preview(self.ctx, self.repo, self.sha)
                stop = lifecycle.stop
                calls = 0

                def first_stop(
                    ctx: Ctx, stop: Callable[[Ctx], None] = stop
                ) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("synthetic initial pause failure")
                    stop(ctx)

                with ExitStack() as faults:
                    if point == "pause":
                        faults.enter_context(
                            mock.patch.object(lifecycle, "stop", first_stop)
                        )
                    elif point == "pin":
                        faults.enter_context(
                            mock.patch.object(
                                steps_release,
                                "relink",
                                side_effect=OSError("synthetic pin failure"),
                            )
                        )
                    else:
                        self.world.fake.doctor = 1
                    with self.assertRaisesRegex(
                        AssertionError, "actual renewal entry refused"
                    ):
                        self.run_renewal(plan)
                self.world.fake.doctor = 0
                before = load_record(self.ctx.rdir / "rollback-record.json")
                self.assertEqual(before["failed"]["step"], point)
                if point == "pause":
                    self.assertIsNone(before.get("store"))
                    self.assertEqual(
                        before["host_renewal"]["phase"], "approved_intent"
                    )
                result = self.run_renewal(plan)
                self.assertTrue(result["install_complete"])
                self.assertEqual(
                    result["host_renewal"]["prior_summary"],
                    before["host_renewal"]["prior_summary"],
                )
                control = self.read_control()
                self.assertEqual(control, result["host_renewal"]["successor"])
                self.run_renewal(plan)
                self.assertEqual(self.read_control(), control)

    def pause_failure(self) -> HostPlan:
        """Retain an authentic first-pause failure with real fake cleanup."""
        plan = preview(self.ctx, self.repo, self.sha)
        stop = lifecycle.stop
        calls = 0

        def first_stop(ctx: Ctx) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic initial pause failure")
            stop(ctx)

        with (
            mock.patch.object(lifecycle, "stop", first_stop),
            self.assertRaisesRegex(
                AssertionError, "actual renewal entry refused"
            ),
        ):
            self.run_renewal(plan)
        return plan

    def interrupt_retry(self, plan: HostPlan, point: str) -> Record:
        """Interrupt a real retry around its existing durable phase writes."""
        original = host_renewal_receipts.phase

        def crash(
            ctx: Ctx, rec: Record, name: str, **fields: object
        ) -> RenewalReceipt:
            recognition = fields.get("recognized_summary") is not None
            if point == "before_recognized" and recognition:
                raise KeyboardInterrupt("synthetic before recognition")
            value = original(ctx, rec, name, **fields)
            if (point == "recognized" and recognition) or name == point:
                raise KeyboardInterrupt("synthetic durable interruption")
            return value

        with (
            mock.patch.object(host_renewal, "phase", crash),
            mock.patch.object(host_renewal_receipts, "phase", crash),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_renewal(plan)
        return load_record(self.ctx.rdir / "rollback-record.json")

    def test_summary_proof_survives_interrupted_metadata_advance(self) -> None:
        for point in (
            "before_recognized",
            "recognized",
            "contained_archived",
            "grant_committed",
        ):
            with self.subTest(point=point):
                self.doCleanups()
                self.setUp()
                plan = self.pause_failure()
                initial = load_record(self.ctx.rdir / "rollback-record.json")
                path = self.ctx.lib / "install-record.json"
                raw = path.read_bytes()
                saved = self.interrupt_retry(plan, point)
                proof = saved["host_renewal"]["recognized_summary"]
                if point == "before_recognized":
                    self.assertIsNone(proof)
                else:
                    self.assertEqual(
                        proof["sha256"], hashlib.sha256(raw).hexdigest()
                    )
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(
                    saved["host_renewal"]["prior_summary"],
                    initial["host_renewal"]["prior_summary"],
                )
                result = self.run_renewal(plan)
                self.assertTrue(result["install_complete"])
                control = self.read_control()
                self.assertEqual(control, result["host_renewal"]["successor"])
                self.run_renewal(plan)
                self.assertEqual(self.read_control(), control)

    def test_changed_recognized_summary_refuses_before_more_effects(
        self,
    ) -> None:
        for point in ("recognized", "contained_archived"):
            for change in ("foreign", "extra", "wrong_type", "unknown_error"):
                with self.subTest(point=point, change=change):
                    self.doCleanups()
                    self.setUp()
                    plan = self.pause_failure()
                    saved = self.interrupt_retry(plan, point)
                    path = self.ctx.lib / "install-record.json"
                    value = json.loads(path.read_bytes())
                    if change == "foreign":
                        value["ts"] = "20261101T000000Z"
                    elif change == "extra":
                        value["extra"] = "synthetic untrusted value"
                    elif change == "wrong_type":
                        value["host_approval_committed"] = 0
                    else:
                        value["failed"]["error"] = "unknown_error"
                    raw = json.dumps(value).encode()
                    path.write_bytes(raw)
                    control = self.read_control()
                    with (
                        mock.patch.object(
                            lifecycle,
                            "stop",
                            side_effect=AssertionError("stopped"),
                        ),
                        self.assertRaisesRegex(
                            AssertionError, "actual renewal entry refused"
                        ),
                    ):
                        self.run_renewal(plan)
                    self.assertEqual(self.read_control(), control)
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(
                        load_record(self.ctx.rdir / "rollback-record.json"),
                        saved,
                    )
