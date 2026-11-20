"""A Codex interface change keeps going when a notice gets a new time."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest import mock

from install import codex_interface
from install.codex_interface_models import InterfacePlan
from install.codex_interface_preview import preview
from install.context import Ctx, StepFailedError
from install.record import Record, load_record
from muninn import review_recovery, store
from muninn.review_progress import digest
from tests.codex_interface_support import InterfaceApplyFixture

RECORD = "rollback-record.json"


class InterfaceNoticeFixture(InterfaceApplyFixture):
    """Write setup notices and read the learning rule of the store."""

    def notice(self, code: str, now: float) -> None:
        """Write the setup notice as the running service writes it.

        Args:
            code: The cause code of the notice.
            now: The time that the service gives to the notice.
        """
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            review_recovery.record(conn, "setup", code, "launch", now)
            conn.execute("COMMIT")

    def rule(self) -> str:
        """Read the digest of the learning rule of the store.

        Returns:
            The policy digest of the review control row.
        """
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT policy_digest FROM review_control"
            ).fetchone()
        return str(row[0])

    def prepared(self) -> InterfacePlan:
        """Make one old request and preview the change with a notice.

        Returns:
            The plan of the preview.
        """
        self.managed_release()
        self.request()
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        return plan

    def apply_with_notice(
        self, plan: InterfacePlan, code: str, now: float
    ) -> Record:
        """Apply the plan while the service writes a notice after approval.

        Args:
            plan: The approved plan.
            code: The cause code of the notice that the service writes.
            now: The time of that notice.

        Returns:
            The final installer record.
        """
        original = codex_interface.run

        def service(ctx: Ctx, rec: Record, approved: InterfacePlan) -> None:
            original(ctx, rec, approved)
            self.notice(code, now)

        with mock.patch.object(codex_interface, "run", side_effect=service):
            return self.apply(plan)

    def assert_stopped_before_rule(self) -> None:
        """Require the old rule and an intent with no owner control."""
        self.assertEqual(self.rule(), digest(self.old))
        saved = load_record(self.ctx.rdir / RECORD)
        value = saved["codex_interface"]
        self.assertEqual(value["phase"], "approved_intent")
        self.assertIsNone(value.get("owner_control"))
        self.assertIsNone(value.get("contained"))


class InterfaceNoticeTests(InterfaceNoticeFixture):
    """Only the time of the notice may change between preview and pause."""

    def test_new_time_before_apply_keeps_the_digest(self) -> None:
        plan = self.prepared()
        self.notice("capability_changed", 2.0)
        self.assertEqual(preview(self.ctx).digest, plan.digest)
        result = self.apply(plan)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        self.assertEqual(self.rule(), digest(plan.candidate))

    def test_new_time_after_approval_completes(self) -> None:
        plan = self.prepared()
        result = self.apply_with_notice(plan, "capability_changed", 5.0)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        self.assertEqual(self.rule(), digest(plan.candidate))
        self.assertIsNotNone(self.world.fake.loaded)

    def test_changed_code_after_approval_stops_without_rule(self) -> None:
        plan = self.prepared()
        with self.assertRaises(StepFailedError):
            self.apply_with_notice(plan, "auth", 5.0)
        self.assert_stopped_before_rule()

    def test_changed_code_before_apply_stops_without_rule(self) -> None:
        plan = self.prepared()
        self.notice("auth", 2.0)
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_plan_changed"
        ):
            self.apply(plan)
        self.assertEqual(self.rule(), digest(self.old))

    def test_new_notice_after_approval_stops_without_rule(self) -> None:
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        with self.assertRaises(StepFailedError):
            self.apply_with_notice(plan, "auth", 5.0)
        self.assert_stopped_before_rule()

    def test_removed_notice_after_approval_stops_without_rule(self) -> None:
        plan = self.prepared()
        original = codex_interface.run

        def service(ctx: Ctx, rec: Record, approved: InterfacePlan) -> None:
            original(ctx, rec, approved)
            self.sql("DELETE FROM review_issue WHERE id='setup'")

        with (
            mock.patch.object(codex_interface, "run", side_effect=service),
            self.assertRaises(StepFailedError),
        ):
            self.apply(plan)
        self.assert_stopped_before_rule()
