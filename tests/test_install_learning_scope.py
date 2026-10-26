"""Check installer learning choices with a disposable store."""

from __future__ import annotations

from unittest import mock

from install import learning_scope
from install.context import Ctx, StepFailedError
from muninn import review_state
from tests.test_review_requests import RequestCase


class InstallLearningScopeTests(RequestCase):
    """Require a choice and retain saved choices."""

    def context(self) -> Ctx:
        """Build an isolated installer context without native side effects."""
        ctx = Ctx(
            self.tmp, mock.Mock(), "test", platform="darwin", now=lambda: 20.0
        )
        ctx.data = self.home
        ctx.say = mock.Mock()
        return ctx

    def test_from_now_is_saved_at_the_install_boundary(self) -> None:
        ctx = self.context()
        ctx.learning_mode = "now"
        learning_scope.apply(ctx, {}, self.conn)
        chosen = review_state.selection(self.conn)
        assert chosen is not None
        self.assertEqual(chosen.selected_at, 20.0)
        self.assertEqual(chosen.mode, "now")

    def test_all_history_requires_confirmation_after_scan(self) -> None:
        ctx = self.context()
        ctx.learning_mode = "all"
        ctx.learning_prompt = mock.Mock(return_value="no")
        with self.assertRaisesRegex(StepFailedError, "history_not_confirmed"):
            learning_scope.apply(ctx, {}, self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            0,
        )
        ctx.learning_prompt = mock.Mock(return_value="yes")
        learning_scope.apply(ctx, {}, self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            1,
        )

    def test_dry_run_does_not_select_a_mode(self) -> None:
        ctx = self.context()
        ctx.dry_run = True
        prompt = mock.Mock(side_effect=AssertionError("dry run prompted"))
        learning_scope.choose(ctx, None, prompt)
        self.assertIsNone(ctx.learning_mode)
        self.assertIsNone(review_state.selection(self.conn))
