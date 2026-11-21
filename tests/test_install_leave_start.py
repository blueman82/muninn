"""Leave a real unfinished upgrade start only when nothing needs undo."""

from __future__ import annotations

import dataclasses
import json
import unittest
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import installer, leave_start, record, review_policy
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import store
from tests.installer_support import World, git
from tests.test_review_shared_models import policy_data

RECORD = "rollback-record.json"


def tree(home: Path) -> dict[str, bytes]:
    """Read every regular file and every link under a temporary home.

    Args:
        home: The temporary home of one test.

    Returns:
        File bytes and link targets keyed by the relative path.
    """
    found: dict[str, bytes] = {}
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home).as_posix()
        if path.is_symlink():
            found[rel] = b"link:" + str(path.readlink()).encode()
        elif path.is_file():
            found[rel] = path.read_bytes()
    return found


def no_prompt(question: str) -> str:
    """Fail when the leave asks a question that it must not ask.

    Args:
        question: The question text.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError(f"unexpected question: {question}")


def write_policy(home: Path) -> Path:
    """Write one valid review policy file into a temporary home.

    Args:
        home: The temporary home of one test.

    Returns:
        The path of the policy file.
    """
    path = home / "policy.json"
    data = policy_data()
    data["projection_version"] = "permitted-fields-v2"
    path.write_text(json.dumps(data))
    return path


def stop_after_intent(tc: unittest.TestCase, ctx: Ctx, repo: Path) -> None:
    """Run a real start that stops after its durable activation intent.

    Args:
        tc: The running test.
        ctx: The context of the start, with a review policy.
        repo: The repository to install.
    """
    original = record.save
    stopped = False

    def save(active: Ctx, rec: Record) -> None:
        nonlocal stopped
        original(active, rec)
        phase = rec.get("policy_activation", {}).get("phase")
        if phase == "intent" and not stopped:
            stopped = True
            raise OSError("stopped after intent")

    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    with (
        mock.patch("install.record.save", side_effect=save),
        tc.assertRaisesRegex(OSError, "stopped after intent"),
    ):
        installer.install(ctx, repo, sha)
    tc.assertTrue(stopped)


class LeaveStartTests(unittest.TestCase):
    """Leave one upgrade start that the real installer engine stopped."""

    def setUp(self) -> None:
        self.world = World(self)
        self.base = self.world.ctx(fresh=True)
        installer.install(self.base, self.world.repo, self.world.sha)
        self.start = dataclasses.replace(
            self.base,
            fresh=False,
            upgrade=True,
            ts="start",
            review_policy=write_policy(self.base.home),
        )
        self.path = self.start.rdir / RECORD

    def later(self, *, dry_run: bool = False) -> Ctx:
        """Build the context of a later run without a policy file.

        Args:
            dry_run: True for the ``--check`` form.

        Returns:
            An upgrade context with a new operation time stamp.
        """
        return dataclasses.replace(
            self.base,
            fresh=False,
            upgrade=True,
            ts="later",
            review_policy=None,
            dry_run=dry_run,
        )

    def stop(self) -> None:
        """Stop the upgrade start after intent and before its commit."""
        stop_after_intent(self, self.start, self.world.repo)
        saved = record.load_record(self.path)
        self.assertEqual(saved["policy_activation"]["phase"], "intent")
        self.assertEqual(self.state(), (0, None, None))
        self.assertIsNotNone(review_policy.pending(self.later()))

    def state(self) -> tuple[object, ...]:
        """Observe admission and the first-policy state of the store."""
        with closing(store.connect_ro(store.db_path(self.base.data))) as conn:
            return tuple(
                conn.execute(
                    "SELECT admission,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )

    def assert_refused(self, code: str) -> None:
        """Require one refusal before any question and no file change.

        Args:
            code: The expected refusal code.
        """
        before = tree(self.base.home)
        with self.assertRaisesRegex(StepFailedError, code):
            leave_start.leave(self.later(), no_prompt)
        self.assertEqual(tree(self.base.home), before)
        self.assertIsNotNone(review_policy.pending(self.later()))

    def test_leave_after_the_real_stops_lets_an_upgrade_finish(
        self,
    ) -> None:
        self.stop()
        settings = json.loads(self.base.settings.read_text())
        settings["theme"] = "light"
        self.base.settings.write_text(json.dumps(settings, indent=2))
        git(self.world.repo, "commit", "--allow-empty", "-qm", "next")
        second = git(self.world.repo, "rev-parse", "HEAD").decode().strip()
        before = tree(self.base.home)
        saved = record.load_record(self.path)
        questions: list[str] = []

        def prompt(question: str) -> str:
            questions.append(question)
            return "yes"

        self.assertEqual(leave_start.leave(self.later(), prompt), "left")
        self.assertEqual(len(questions), 1)
        self.assertTrue(questions[0].endswith("[y/N] "))
        after = tree(self.base.home)
        rel = self.path.relative_to(self.base.home).as_posix()
        self.assertNotEqual(after.pop(rel), before.pop(rel))
        self.assertEqual(after, before)
        marked = record.load_record(self.path)
        self.assertEqual(marked.pop("superseded_by"), "left_unfinished")
        self.assertEqual(marked, saved)
        self.assertEqual(self.state(), (0, None, None))
        self.assertIsNone(review_policy.pending(self.later()))
        raw = self.path.read_bytes()
        self.assertEqual(leave_start.leave(self.later(), no_prompt), "nothing")
        self.assertEqual(self.path.read_bytes(), raw)
        installer.install(self.later(), self.world.repo, second)
        current = (self.base.lib / "current").readlink()
        self.assertEqual(current.name, second)
        self.assertIsNone(review_policy.pending(self.later()))

    def test_upper_case_answer_is_a_yes(self) -> None:
        self.stop()
        result = leave_start.leave(self.later(), lambda _: " Y ")
        self.assertEqual(result, "left")
        self.assertIsNone(review_policy.pending(self.later()))

    def test_answer_that_is_not_yes_changes_nothing(self) -> None:
        self.stop()
        before = tree(self.base.home)
        prompts: list[Callable[[str], str] | None] = [
            lambda _: "no",
            lambda _: "",
            lambda _: "yes please",
            None,
        ]
        for prompt in prompts:
            with (
                self.subTest(prompt=prompt),
                self.assertRaisesRegex(
                    StepFailedError, "leave_confirmation_required"
                ),
            ):
                leave_start.leave(self.later(), prompt)
        self.assertEqual(tree(self.base.home), before)
        self.assertIsNotNone(review_policy.pending(self.later()))

    def test_end_of_input_changes_nothing(self) -> None:
        self.stop()
        before = tree(self.base.home)

        def closed(question: str) -> str:
            raise EOFError(question)

        with self.assertRaisesRegex(
            StepFailedError, "leave_confirmation_required"
        ):
            leave_start.leave(self.later(), closed)
        self.assertEqual(tree(self.base.home), before)

    def test_check_shows_the_plan_and_writes_nothing(self) -> None:
        self.stop()
        before = tree(self.base.home)
        said = len(self.world.out)
        result = leave_start.leave(self.later(dry_run=True), no_prompt)
        self.assertEqual(result, "would_leave")
        self.assertEqual(tree(self.base.home), before)
        lines = self.world.out[said:]
        self.assertTrue(any(line.startswith("DRY-RUN:") for line in lines))
        self.assertFalse(any(str(self.base.home) in line for line in lines))
        self.assertIsNotNone(review_policy.pending(self.later()))

    def test_link_with_a_third_value_refuses(self) -> None:
        self.stop()
        links = record.load_record(self.path)["links"]
        self.assertIn(str(self.base.muninn), links)
        self.base.muninn.unlink()
        self.base.muninn.symlink_to(self.base.home / "another-muninn")
        self.assert_refused("leave_links_changed")

    def test_missing_release_of_the_start_refuses(self) -> None:
        self.stop()
        sha = record.load_record(self.path)["sha"]
        (self.base.lib / sha).rename(self.base.lib / "moved-release")
        self.assert_refused("leave_links_changed")

    def test_unreadable_provider_file_refuses(self) -> None:
        self.stop()
        self.base.settings.write_text("{not json")
        self.assert_refused("leave_provider_unreadable")

    def test_committed_policy_refuses(self) -> None:
        original = record.save

        def save(active: Ctx, rec: Record) -> None:
            if rec.get("policy_status") == "active":
                raise OSError("completion save")
            original(active, rec)

        with (
            mock.patch("install.record.save", side_effect=save),
            self.assertRaisesRegex(OSError, "completion save"),
        ):
            installer.install(self.start, self.world.repo, self.world.sha)
        self.assertNotEqual(self.state(), (0, None, None))
        self.assert_refused("leave_policy_active")

    def test_nothing_to_leave_changes_nothing(self) -> None:
        before = tree(self.base.home)
        result = leave_start.leave(self.later(), no_prompt)
        self.assertEqual(result, "nothing")
        self.assertEqual(tree(self.base.home), before)


class LeaveFreshStartTests(unittest.TestCase):
    """A fresh start has no earlier install that a new upgrade can use."""

    def test_fresh_start_is_unsupported(self) -> None:
        world = World(self)
        ctx = world.ctx(fresh=True)
        ctx = dataclasses.replace(ctx, review_policy=write_policy(ctx.home))
        stop_after_intent(self, ctx, world.repo)
        later = dataclasses.replace(ctx, review_policy=None)
        self.assertIsNotNone(review_policy.pending(later))
        before = tree(world.home)
        with self.assertRaisesRegex(
            StepFailedError, "leave_record_unsupported"
        ):
            leave_start.leave(later, no_prompt)
        self.assertEqual(tree(world.home), before)


if __name__ == "__main__":
    unittest.main()
