"""A setup notice with only a new time keeps the scan and the renewal."""

from __future__ import annotations

from contextlib import closing
from unittest import mock

from install import host_renewal
from install.context import Ctx, StepFailedError
from install.host_renewal_models import HostPlan
from install.host_renewal_preview import preview
from install.legacy_state import OperationCheck
from install.record import Record
from muninn import review_recovery, store
from tests.host_renewal_support import HostFixture


class SetupNoticeFixture(HostFixture):
    """Write setup notices with the writer API that the service uses."""

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


class SetupNoticeScanTests(SetupNoticeFixture):
    """The scan binds the meaning of the notice, not its time."""

    def test_new_time_keeps_plan_digest_and_scan(self) -> None:
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        self.notice("capability_changed", 2.0)
        self.assertEqual(preview(self.ctx, self.repo, self.sha), plan)
        after = self.scan(plan.containment)
        self.assertEqual(after.state.work_digest, plan.state.work_digest)
        self.assertEqual(
            after.containment.invariant_digest,
            plan.containment.invariant_digest,
        )

    def test_changed_code_stops_scan_and_changes_plan(self) -> None:
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        self.notice("auth", 1.0)
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_changed"
        ):
            self.scan(plan.containment)
        changed = preview(self.ctx, self.repo, self.sha)
        self.assertNotEqual(changed.digest, plan.digest)

    def test_each_other_notice_column_is_bound(self) -> None:
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        for column in ("stage", "cause", "action"):
            with self.subTest(column=column):
                self.sql(
                    f"UPDATE review_issue SET {column}={column}||'x' "
                    "WHERE id='setup'"
                )
                with self.assertRaisesRegex(
                    StepFailedError, "host_renewal_state_changed"
                ):
                    self.scan(plan.containment)
                self.notice("capability_changed", 1.0)

    def test_removed_notice_stops_scan(self) -> None:
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        self.sql("DELETE FROM review_issue WHERE id='setup'")
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_changed"
        ):
            self.scan(plan.containment)

    def test_new_notice_stops_scan(self) -> None:
        plan = preview(self.ctx, self.repo, self.sha)
        self.notice("auth", 3.0)
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_state_changed"
        ):
            self.scan(plan.containment)


class SetupNoticeRenewalTests(SetupNoticeFixture):
    """A host renewal continues only for a notice with the same meaning."""

    def test_renewal_continues_after_new_time_before_apply(self) -> None:
        self.notice("codex_experimental_boundary", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        self.notice("codex_experimental_boundary", 2.0)
        result = self.run_renewal(plan)
        self.assertEqual(
            result["host_renewal"]["setup_notice"]["code"],
            "codex_experimental_boundary",
        )

    def test_renewal_continues_after_new_time_after_intent(self) -> None:
        self.notice("codex_experimental_boundary", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        original = host_renewal.stage

        def service(
            ctx: Ctx, rec: Record, approved: HostPlan, check: OperationCheck
        ) -> None:
            original(ctx, rec, approved, check)
            self.notice("codex_experimental_boundary", 5.0)

        with mock.patch.object(host_renewal, "stage", side_effect=service):
            result = self.run_renewal(plan)
        self.assertEqual(
            result["host_renewal"]["setup_notice"]["code"],
            "codex_experimental_boundary",
        )

    def test_renewal_stops_after_changed_code(self) -> None:
        self.notice("codex_experimental_boundary", 1.0)
        plan = preview(self.ctx, self.repo, self.sha)
        self.notice("auth", 1.0)
        before = self.read_control()
        with self.assertRaisesRegex(
            AssertionError, "actual renewal entry refused"
        ):
            self.run_renewal(plan)
        self.assertEqual(self.read_control(), before)
