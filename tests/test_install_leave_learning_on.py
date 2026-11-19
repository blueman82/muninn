"""Leave an unfinished upgrade start when the store has a learning policy."""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

from install import (
    installer,
    learning_scope,
    leave_start,
    record,
    review_policy,
)
from install.context import Ctx, StepFailedError
from install.review_policy_parse import policy as parse_policy
from muninn import review_history, review_state, store
from muninn.review_activation import bootstrap_policy
from muninn.review_state import LearningSelection
from tests.installer_support import World, git
from tests.test_install_leave_start import (
    no_prompt,
    stop_after_intent,
    tree,
    write_policy,
)

RECORD = "rollback-record.json"


def turn_on(data: Path, raw: str, activated_at: float) -> None:
    """Commit a first policy into the store as one real bootstrap does.

    With the activation time of the intent of the start, this is the
    commit of the start itself: the start stopped after its commit and
    before it saved phase ``complete``.

    Args:
        data: The Muninn data folder of the test home.
        raw: The policy text.
        activated_at: The activation time to store.
    """
    policy = parse_policy(raw)
    with closing(store.connect_rw(store.db_path(data))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        bootstrap_policy(conn, policy, activated_at)
        conn.execute("COMMIT")


def save_scope(data: Path, raw: str, selected_at: float) -> None:
    """Save the scope From now as an earlier learning start does.

    Args:
        data: The Muninn data folder of the test home.
        raw: The policy text of the store.
        selected_at: The time of the earlier scope choice.
    """
    policy = parse_policy(raw)
    with closing(store.connect_rw(store.db_path(data))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        review_history.configure(conn, policy, "now", None, selected_at, None)
        conn.execute("COMMIT")


def no_terminal(question: str) -> str:
    """Act as a run with no terminal: each question gets end of input.

    Args:
        question: The question text.

    Raises:
        EOFError: Always.
    """
    del question
    raise EOFError


def change_store(data: Path, sql: str) -> None:
    """Run one change on the review control row of the test store.

    Args:
        data: The Muninn data folder of the test home.
        sql: One UPDATE statement.
    """
    with closing(store.connect_rw(store.db_path(data))) as conn:
        conn.execute(sql)
        conn.commit()


def yes(question: str) -> str:
    """Answer yes to the one question of the leave.

    Args:
        question: The question text.

    Returns:
        The answer ``y``.
    """
    del question
    return "y"


class LearningOnTests(unittest.TestCase):
    """Leave an ``--enable-learning`` start when the store has a policy."""

    def setUp(self) -> None:
        """Install once, then stop an upgrade start after its intent."""
        self.world = World(self)
        self.base = self.world.ctx(fresh=True)
        installer.install(self.base, self.world.repo, self.world.sha)
        policy_path = write_policy(self.base.home)
        self.raw = policy_path.read_text()
        self.start = dataclasses.replace(
            self.base,
            fresh=False,
            upgrade=True,
            ts="start",
            review_policy=policy_path,
            enable_learning=True,
        )
        self.path = self.start.rdir / RECORD
        stop_after_intent(self, self.start, self.world.repo)
        saved = record.load_record(self.path)
        self.assertEqual(saved["policy_activation"]["phase"], "intent")
        self.intent_at = cast(
            float,
            saved["policy_activation"]["intent"]["control"]["activated_at"],
        )
        self.assertEqual(self.state(), (0, None, None))

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
            enable_learning=False,
            dry_run=dry_run,
        )

    def state(self) -> tuple[object, ...]:
        """Observe admission and the first-policy state of the store.

        Returns:
            Admission, policy digest and activation time.
        """
        with closing(store.connect_ro(store.db_path(self.base.data))) as conn:
            return tuple(
                conn.execute(
                    "SELECT admission,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )

    def policy_json(self) -> object:
        """Read the stored policy text.

        Returns:
            The ``policy_json`` value of the store.
        """
        with closing(store.connect_ro(store.db_path(self.base.data))) as conn:
            return conn.execute(
                "SELECT policy_json FROM review_control"
            ).fetchone()[0]

    def case(self) -> str:
        """Find the case of the pending start with the public function.

        Returns:
            The case that ``leave_start.policy_case`` returns.
        """
        found = review_policy.pending(self.later())
        assert found is not None
        return leave_start.policy_case(found[0], found[1])

    def check_lines(self) -> list[str]:
        """Run the ``--check`` form, require no change, return its lines.

        Returns:
            The printed lines.
        """
        before = tree(self.base.home)
        said = len(self.world.out)
        result = leave_start.leave(self.later(dry_run=True), no_prompt)
        self.assertEqual(result, "would_leave")
        self.assertEqual(tree(self.base.home), before)
        lines = self.world.out[said:]
        self.assertTrue(any(line.startswith("DRY-RUN:") for line in lines))
        self.assertFalse(any(str(self.base.home) in line for line in lines))
        return lines

    def scope(self) -> LearningSelection | None:
        """Read the saved learning scope of the store.

        Returns:
            The saved scope, or None when the store has no scope.
        """
        with closing(store.connect_ro(store.db_path(self.base.data))) as conn:
            conn.row_factory = sqlite3.Row
            return review_state.selection(conn)

    def leave_keeps_store(self) -> None:
        """Leave the start and require that only its record changes."""
        on = self.state()
        policy = self.policy_json()
        before = tree(self.base.home)
        saved = record.load_record(self.path)
        self.assertEqual(leave_start.leave(self.later(), yes), "left")
        after = tree(self.base.home)
        rel = self.path.relative_to(self.base.home).as_posix()
        after.pop(rel)
        before.pop(rel)
        self.assertEqual(after, before)
        marked = record.load_record(self.path)
        self.assertEqual(marked.pop("superseded_by"), "left_unfinished")
        self.assertEqual(marked, saved)
        self.assertEqual(self.state(), on)
        self.assertEqual(self.policy_json(), policy)
        self.assertIsNone(review_policy.pending(self.later()))

    def upgrade(
        self, requested: str | None, prompt: Callable[[str], str]
    ) -> Ctx:
        """Choose the scope and run the next upgrade as the entry does.

        Args:
            requested: The value of ``--learning-scope``, or None.
            prompt: The terminal of the run.

        Returns:
            The context of the upgrade.
        """
        ctx = self.later()
        learning_scope.choose(ctx, requested, prompt)
        git(self.world.repo, "commit", "--allow-empty", "-qm", "next")
        second = git(self.world.repo, "rev-parse", "HEAD").decode().strip()
        installer.install(ctx, self.world.repo, second)
        current = (self.base.lib / "current").readlink()
        self.assertEqual(current.name, second)
        self.assertIsNone(review_policy.pending(self.later()))
        return ctx

    def assert_refused(self) -> None:
        """Require ``leave_policy_active`` with no question and no change."""
        before = tree(self.base.home)
        state = self.state()
        for ctx in (self.later(dry_run=True), self.later()):
            with (
                self.subTest(dry_run=ctx.dry_run),
                self.assertRaisesRegex(StepFailedError, "leave_policy_active"),
            ):
                leave_start.leave(ctx, no_prompt)
        with self.assertRaisesRegex(StepFailedError, "leave_policy_active"):
            self.case()
        self.assertEqual(tree(self.base.home), before)
        self.assertEqual(self.state(), state)
        self.assertIsNotNone(review_policy.pending(self.later()))

    def test_no_policy_is_the_pr_97_case(self) -> None:
        """A start that committed nothing is the case with no policy."""
        self.assertEqual(self.case(), "no_policy")

    def test_check_names_each_case_in_different_words(self) -> None:
        """The ``--check`` lines differ for each of the three cases."""
        none_lines = set(self.check_lines())
        turn_on(self.base.data, self.raw, self.intent_at - 60.0)
        self.assertEqual(self.case(), "earlier_policy")
        earlier_lines = set(self.check_lines())
        change_store(
            self.base.data,
            f"UPDATE review_control SET activated_at={self.intent_at!r}",
        )
        self.assertEqual(self.case(), "own_policy")
        own_lines = set(self.check_lines())
        self.assertTrue(earlier_lines - none_lines)
        self.assertTrue(own_lines - none_lines)
        self.assertTrue(own_lines - earlier_lines)
        self.assertIsNotNone(review_policy.pending(self.later()))

    def test_earlier_policy_is_left_and_learning_stays_on(self) -> None:
        """Case A: leave, then upgrade with the saved scope, no question."""
        turn_on(self.base.data, self.raw, self.intent_at - 86400.0)
        save_scope(self.base.data, self.raw, self.intent_at - 86400.0)
        self.assertEqual(self.state()[0], 1)
        self.assertEqual(self.case(), "earlier_policy")
        on = self.state()
        policy = self.policy_json()
        scope = self.scope()
        self.assertIsNotNone(scope)
        self.leave_keeps_store()
        ctx = self.upgrade(None, no_prompt)
        self.assertTrue(ctx.retained_learning_scope)
        self.assertEqual(self.state(), on)
        self.assertEqual(self.policy_json(), policy)
        self.assertEqual(self.scope(), scope)

    def test_own_committed_policy_is_left_and_learning_stays_on(self) -> None:
        """Case B: the start committed and did not save phase complete."""
        turn_on(self.base.data, self.raw, self.intent_at)
        saved = record.load_record(self.path)
        self.assertEqual(saved["policy_activation"]["phase"], "intent")
        self.assertEqual(self.state()[0], 1)
        self.assertEqual(self.state()[2], self.intent_at)
        self.assertEqual(self.case(), "own_policy")
        self.assertIsNone(self.scope())
        self.leave_keeps_store()
        self.assertIsNone(self.scope())

    def test_own_policy_upgrade_asks_the_scope_again(self) -> None:
        """Case B: the upgrade needs a scope; with no answer nothing changes.

        The start stopped before it saved its scope, so the scope choice
        of the start is not in the store or in the record.
        """
        turn_on(self.base.data, self.raw, self.intent_at)
        self.leave_keeps_store()
        on = self.state()
        policy = self.policy_json()
        before = tree(self.base.home)
        with self.assertRaisesRegex(
            StepFailedError, "learning_scope_required"
        ):
            learning_scope.choose(self.later(), None, no_terminal)
        self.assertEqual(tree(self.base.home), before)
        ctx = self.upgrade("now", no_prompt)
        self.assertEqual(ctx.learning_mode, "now")
        self.assertEqual(self.state(), on)
        self.assertEqual(self.policy_json(), policy)
        scope = self.scope()
        assert scope is not None
        self.assertEqual(scope.mode, "now")

    def test_own_policy_upgrade_takes_the_scope_answer(self) -> None:
        """Case B: an answer at the terminal is the scope of the upgrade."""
        turn_on(self.base.data, self.raw, self.intent_at)
        self.leave_keeps_store()
        on = self.state()
        self.upgrade(None, lambda question: "n")
        self.assertEqual(self.state(), on)
        scope = self.scope()
        assert scope is not None
        self.assertEqual(scope.mode, "now")

    def test_paused_earlier_policy_is_left_and_stays_paused(self) -> None:
        """An earlier policy with admission 0 stays as it was."""
        turn_on(self.base.data, self.raw, self.intent_at - 60.0)
        change_store(self.base.data, "UPDATE review_control SET admission=0")
        paused = self.state()
        self.assertEqual(leave_start.leave(self.later(), yes), "left")
        self.assertEqual(self.state(), paused)
        self.assertIsNone(review_policy.pending(self.later()))

    def test_paused_own_policy_is_left_and_stays_paused(self) -> None:
        """A committed own policy that the person paused stays paused."""
        turn_on(self.base.data, self.raw, self.intent_at)
        change_store(self.base.data, "UPDATE review_control SET admission=0")
        paused = self.state()
        self.assertEqual(leave_start.leave(self.later(), yes), "left")
        self.assertEqual(self.state(), paused)
        self.assertIsNone(review_policy.pending(self.later()))

    def test_policy_after_the_start_refuses(self) -> None:
        """A policy that is newer than the start agrees with nothing."""
        turn_on(self.base.data, self.raw, self.intent_at + 60.0)
        self.assert_refused()

    def test_own_policy_with_a_wrong_digest_refuses(self) -> None:
        """A digest that is not the digest of the record refuses."""
        turn_on(self.base.data, self.raw, self.intent_at)
        change_store(
            self.base.data,
            "UPDATE review_control SET policy_digest='" + "0" * 64 + "'",
        )
        self.assert_refused()

    def test_earlier_policy_with_a_wrong_digest_refuses(self) -> None:
        """An earlier policy whose digest does not match its text refuses."""
        turn_on(self.base.data, self.raw, self.intent_at - 60.0)
        change_store(
            self.base.data,
            "UPDATE review_control SET policy_digest='" + "0" * 64 + "'",
        )
        self.assert_refused()

    def test_admission_without_policy_refuses(self) -> None:
        """Open admission with no policy agrees with no case."""
        change_store(self.base.data, "UPDATE review_control SET admission=1")
        self.assert_refused()

    def assert_link_refused(self) -> None:
        """Give the command link a third value and require a refusal."""
        self.base.muninn.unlink()
        self.base.muninn.symlink_to(self.base.home / "another-muninn")
        before = tree(self.base.home)
        with self.assertRaisesRegex(StepFailedError, "leave_links_changed"):
            leave_start.leave(self.later(), no_prompt)
        self.assertEqual(tree(self.base.home), before)

    def test_link_with_a_third_value_refuses_an_earlier_policy(self) -> None:
        """The link proof of ADR 0024 stays for an earlier policy."""
        turn_on(self.base.data, self.raw, self.intent_at - 60.0)
        self.assert_link_refused()

    def test_link_with_a_third_value_refuses_an_own_policy(self) -> None:
        """The link proof of ADR 0024 stays for an own policy."""
        turn_on(self.base.data, self.raw, self.intent_at)
        self.assert_link_refused()

    def test_missing_release_refuses_an_own_policy(self) -> None:
        """The release proof of ADR 0024 stays for an own policy."""
        turn_on(self.base.data, self.raw, self.intent_at)
        sha = record.load_record(self.path)["sha"]
        (self.base.lib / sha).rename(self.base.lib / "moved-release")
        before = tree(self.base.home)
        with self.assertRaisesRegex(StepFailedError, "leave_links_changed"):
            leave_start.leave(self.later(), no_prompt)
        self.assertEqual(tree(self.base.home), before)

    def test_unreadable_provider_file_still_refuses(self) -> None:
        """The provider proof of ADR 0024 stays for an own policy."""
        turn_on(self.base.data, self.raw, self.intent_at)
        self.base.settings.write_text("{not json")
        before = tree(self.base.home)
        with self.assertRaisesRegex(
            StepFailedError, "leave_provider_unreadable"
        ):
            leave_start.leave(self.later(), no_prompt)
        self.assertEqual(tree(self.base.home), before)


if __name__ == "__main__":
    unittest.main()
