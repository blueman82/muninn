"""Continue or leave a Codex interface change that stopped at the store."""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import (
    codex_interface,
    codex_interface_recovery,
    failure_text,
    installer,
    leave_start,
)
from install.codex_interface_models import InterfacePlan
from install.codex_interface_preview import preview
from install.context import Ctx, StepFailedError
from install.record import Record, load_record
from muninn.review_progress import digest
from tests.installer_support import git
from tests.test_codex_interface_notice import InterfaceNoticeFixture
from tests.test_install_leave_start import no_prompt, tree

RECORD = "rollback-record.json"
GUIDE = Path(__file__).resolve().parent.parent / "docs" / "USER-GUIDE.md"
Jobs = list[tuple[str, str]]


class StoppedInterfaceFixture(InterfaceNoticeFixture):
    """Make the stopped change of the facts with two abandoned attempts."""

    def abandon(self, request: str, attempt: str) -> None:
        """Give one attempt the shape that a stop of the service gives.

        Args:
            request: The request of the attempt.
            attempt: The attempt that the service abandons.
        """
        ended = 3.0
        backoff = self.old.retry_backoff_seconds
        retry = ended + backoff[0] if self.old.max_attempts > 1 else None
        self.sql(
            "UPDATE review_attempt SET ended_at=?,progress_at=?,"
            "outcome='abandoned' WHERE id=?",
            (ended, ended, attempt),
        )
        self.sql(
            "UPDATE review_request SET state=?,retry_at=?,"
            "current_attempt_id=NULL WHERE id=?",
            ("waiting" if retry is not None else "failed", retry, request),
        )

    def stop_at_store(self) -> tuple[InterfacePlan, Jobs]:
        """Stop one apply at the store check, as the earlier release did.

        Returns:
            The approved plan and the request and attempt of each job.
        """
        self.managed_release()
        jobs: Jobs = []
        for number in (1, 2):
            request = self.request(number=number)
            jobs.append((request, self.attempt(request, number=number)))
        self.notice("capability_changed", 1.0)
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        original = codex_interface.run

        def service(ctx: Ctx, rec: Record, approved: InterfacePlan) -> None:
            original(ctx, rec, approved)
            self.notice("capability_changed", 5.0)
            for request, attempt in jobs:
                self.abandon(request, attempt)

        with (
            mock.patch.object(codex_interface, "run", side_effect=service),
            mock.patch.object(
                codex_interface,
                "before_owner",
                side_effect=StepFailedError("host_renewal_state_changed"),
            ),
            self.assertRaises(StepFailedError),
        ):
            self.apply(plan)
        return plan, jobs

    def other(self, ts: str, *, dry_run: bool) -> Ctx:
        """Copy the context for a later command with no interface plan.

        Args:
            ts: The time stamp of the later command.
            dry_run: Whether the later command only shows its plan.

        Returns:
            The context of the later command.
        """
        later = dataclasses.replace(self.ctx, ts=ts, dry_run=dry_run)
        later.codex_interface = None
        later.interface_plan = None
        return later

    def assert_attempts_kept(self, jobs: Jobs) -> None:
        """Require one abandoned attempt for each request of the facts.

        Args:
            jobs: The request and attempt of each job.
        """
        with closing(sqlite3.connect(self.path)) as conn:
            for request, attempt in jobs:
                rows = conn.execute(
                    "SELECT id,outcome FROM review_attempt "
                    "WHERE request_id=?",
                    (request,),
                ).fetchall()
                self.assertEqual(rows, [(attempt, "abandoned")])


class StoppedInterfaceTests(StoppedInterfaceFixture):
    """A stopped change continues, or the person leaves it safely."""

    def test_stopped_record_has_the_shape_of_the_facts(self) -> None:
        self.stop_at_store()
        saved = load_record(self.ctx.rdir / RECORD)
        value = saved["codex_interface"]
        self.assertEqual(value["phase"], "approved_intent")
        self.assertIsNone(value.get("owner_control"))
        self.assertIsNone(value.get("contained"))
        self.assertTrue(saved["recovery_required"])
        self.assertEqual(saved["failed"]["step"], "prepare_store")
        self.assertIsNone(self.world.fake.loaded)

    def test_same_commit_retry_with_same_digest_completes(self) -> None:
        plan, jobs = self.stop_at_store()
        self.ctx.learning_prompt = lambda _: self.fail(
            "the retry asked for a new approval"
        )
        result = self.apply(plan)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        self.assertEqual(self.rule(), digest(plan.candidate))
        self.assertIsNotNone(self.world.fake.loaded)
        self.assert_attempts_kept(jobs)

    def test_check_names_the_ways_out(self) -> None:
        self.stop_at_store()
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_recovery_required"
        ):
            installer.install(
                self.other("check", dry_run=True), self.repo, self.sha
            )
        text = "\n".join(
            failure_text.explain("codex_interface_recovery_required")
        )
        self.assertIn("--codex-interface", text)
        self.assertIn("bin/muninn-install --leave-unfinished --check", text)

    def test_new_commit_leaves_then_new_plan_completes(self) -> None:
        plan, jobs = self.stop_at_store()
        git(self.repo, "commit", "--allow-empty", "-qm", "fixed installer")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_recovery_required"
        ):
            self.apply(plan)
        self.assertEqual(self.rule(), digest(self.old))
        leaving = self.other("leave", dry_run=False)
        self.assertEqual(leave_start.leave(leaving, lambda _: "yes"), "left")
        saved = load_record(self.ctx.rdir / RECORD)
        self.assertEqual(saved["superseded_by"], "left_unfinished")
        self.assertIsNone(codex_interface_recovery.pending(leaving))
        self.assertEqual(self.rule(), digest(self.old))
        fresh = self.other("after-leave", dry_run=True)
        second = preview(fresh)
        fresh.dry_run = False
        fresh.codex_interface = second.digest
        fresh.interface_plan = second
        fresh.learning_prompt = lambda _: "yes"
        with mock.patch(
            "muninn.review_managed_files.matches", return_value=True
        ):
            result = installer.install(fresh, self.repo, self.sha)
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        self.assertEqual(self.rule(), digest(second.candidate))
        self.assertIsNotNone(self.world.fake.loaded)
        self.assert_attempts_kept(jobs)

    def test_leave_check_writes_nothing(self) -> None:
        self.stop_at_store()
        checking = self.other("leave-check", dry_run=True)
        before = tree(self.home)
        self.assertEqual(leave_start.leave(checking, no_prompt), "would_leave")
        self.assertEqual(tree(self.home), before)
        self.assertIsNotNone(codex_interface_recovery.pending(checking))

    def test_leave_refuses_after_owner_control(self) -> None:
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        with (
            mock.patch.object(
                codex_interface,
                "contained",
                side_effect=StepFailedError("codex_interface_state_changed"),
            ),
            self.assertRaises(StepFailedError),
        ):
            self.apply(plan)
        saved = load_record(self.ctx.rdir / RECORD)
        self.assertIsNotNone(saved["codex_interface"].get("owner_control"))
        leaving = self.other("leave-refused", dry_run=False)
        before = tree(self.home)
        with self.assertRaisesRegex(StepFailedError, "leave_policy_active"):
            leave_start.leave(leaving, no_prompt)
        self.assertEqual(tree(self.home), before)
        self.assertIsNotNone(codex_interface_recovery.pending(leaving))


class InterfaceGuideTest(unittest.TestCase):
    """The user guide has an entry for the stopped Codex interface change."""

    def test_guide_entry_names_the_code(self) -> None:
        text = GUIDE.read_text(encoding="utf-8")
        section = text[text.index("\n## Fix a problem") :]
        self.assertIn("`codex_interface_recovery_required`", section)
        self.assertIn("--leave-unfinished", section)
