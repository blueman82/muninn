"""Retain the original idle wrapper through a temporary host query fault."""

from __future__ import annotations

import time
from collections.abc import Callable
from unittest import mock

from muninn import review_round_launch
from muninn.review_models import ReviewPolicy
from muninn.review_round_state import Round
from muninn.review_runtime_models import (
    ManagedCodexSelection,
    ManagedHostIdentity,
    RunnerConfig,
)
from tests.managed_codex_support import physical_host
from tests.test_review_managed_supervisor import ManagedSupervisorTests


class ReadyHostTests(ManagedSupervisorTests):
    """Use real owned wrappers with fake host IDs and a fake local client."""

    policy_revision = 3
    policy_concurrency = 2

    def defer(self) -> Round:
        """Reach the actual third preparation check with an idle wrapper."""
        with mock.patch(
            "muninn.review_managed_host.observe",
            side_effect=[
                physical_host(),
                physical_host(),
                ValueError("codex_host_identity_unavailable"),
            ],
        ) as host:
            self.until(lambda: host.call_count == 3, 20.0)
        pending = next(iter(self.supervisor._rounds.values()))
        self.assertIsNotNone(pending.ready)
        self.assertFalse(self.marker.exists())
        return pending

    def until(self, predicate: Callable[[], bool], now: float) -> None:
        """Tick within a finite test deadline until the requested condition."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self.supervisor.tick(now)
            if predicate():
                return
            time.sleep(0.01)
        self.fail("owned wrapper did not reach the expected test phase")

    def test_third_query_waits_then_activates_the_same_wrapper(self) -> None:
        original_policy = self.conn.execute(
            "SELECT policy_json FROM review_request"
        ).fetchone()[0]
        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                side_effect=[
                    physical_host(),
                    physical_host(),
                    ValueError("codex_host_identity_unavailable"),
                ],
            ) as host,
            mock.patch.object(
                review_round_launch,
                "cancel_registered",
                wraps=review_round_launch.cancel_registered,
            ) as cancel,
        ):
            self.until(lambda: host.call_count == 3, 20.0)
        cancel.assert_not_called()
        before = tuple(
            self.conn.execute("SELECT * FROM review_call").fetchone()
        )
        pending = next(iter(self.supervisor._rounds.values()))
        handle = pending.handle
        deadline = pending.context.deadline
        if self.carry_legacy:
            self.assertIsInstance(
                ReviewPolicy.model_validate_json(original_policy).runner,
                RunnerConfig,
            )
        self.assertIsInstance(
            pending.context.policy.runner, ManagedCodexSelection
        )
        with mock.patch(
            "muninn.review_managed_host.observe",
            side_effect=AssertionError("cooldown reached host API"),
        ):
            self.supervisor.tick(30.0)
        self.assertEqual(
            tuple(self.conn.execute("SELECT * FROM review_call").fetchone()),
            before,
        )
        with (
            mock.patch(
                "muninn.review_round_launch.create_wrapper",
                side_effect=AssertionError("second wrapper"),
            ),
            mock.patch.object(
                self.supervisor.monitor,
                "activate",
                wraps=self.supervisor.monitor.activate,
            ) as activate,
        ):
            self.supervisor.tick(81.0)
            self.assertIs(pending.handle, handle)
            self.assertEqual(pending.context.deadline, deadline)
            self.finish()
        activate.assert_called_once()
        self.assertEqual(self.marker.read_text().splitlines(), ["0.166.0"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(pending.refresh_count, 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_json FROM review_request"
            ).fetchone()[0],
            original_policy,
        )

    def test_persistent_fault_uses_the_original_deadline(self) -> None:
        pending = self.defer()
        original = pending.context.deadline
        with mock.patch(
            "muninn.review_managed_host.observe",
            side_effect=ValueError("codex_host_identity_unavailable"),
        ) as host:
            self.supervisor.tick(81.0)
        self.assertEqual(host.call_count, 1)
        self.assertEqual(pending.context.deadline, original)
        with (
            mock.patch("time.monotonic", return_value=original + 1),
            mock.patch(
                "muninn.review_managed_host.observe",
                side_effect=AssertionError("expired host query"),
            ),
        ):
            self.supervisor.tick(82.0)
        self.until(lambda: not self.supervisor._rounds, 82.0)
        row = self.conn.execute(
            "SELECT outcome,launched_at FROM review_call"
        ).fetchone()
        self.assertEqual(tuple(row), ("deadline", None))
        self.assertFalse(self.marker.exists())

    def test_revocation_cancels_the_idle_wrapper_during_cooldown(self) -> None:
        pending = self.defer()
        before = tuple(
            self.conn.execute("SELECT * FROM review_request").fetchone()
        )
        self.conn.execute(
            "UPDATE review_control SET admission=0,generation=generation+1"
        )
        control = tuple(
            self.conn.execute("SELECT * FROM review_control").fetchone()
        )
        with mock.patch(
            "muninn.review_managed_host.observe",
            side_effect=AssertionError("revoked host query"),
        ):
            self.until(lambda: not self.supervisor._rounds, 21.0)
        self.assertIsNone(pending.ready)
        self.assertEqual(
            tuple(
                self.conn.execute("SELECT * FROM review_request").fetchone()
            ),
            before,
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT launched_at FROM review_call"
            ).fetchone()[0]
        )
        self.assertFalse(self.marker.exists())
        self.assertEqual(
            tuple(
                self.conn.execute("SELECT * FROM review_control").fetchone()
            ),
            control,
        )

    def test_different_physical_target_at_ready_never_activates(self) -> None:
        with mock.patch(
            "muninn.review_managed_host.observe",
            side_effect=[
                physical_host(),
                physical_host(),
                physical_host("7" * 64),
            ],
        ):
            self.until(
                lambda: self.conn.execute(
                    "SELECT state FROM review_request"
                ).fetchone()[0]
                == "failed",
                20.0,
            )
        self.assertEqual(
            self.conn.execute("SELECT outcome FROM review_call").fetchone()[0],
            "codex_host_changed",
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT launched_at FROM review_call"
            ).fetchone()[0]
        )
        self.assertFalse(self.marker.exists())

    def test_another_round_can_finish_while_ready_waits(self) -> None:
        second = self.enqueue(window=self.window(first=1, last=2))
        self.conn.execute(
            "UPDATE review_request SET created_at=12 WHERE id=?", (second.id,)
        )
        first: list[float] = []
        calls = 0

        def observe(*, deadline: float | None = None) -> ManagedHostIdentity:
            nonlocal calls
            assert deadline is not None
            if not first:
                first.append(deadline)
            if deadline == first[0]:
                calls += 1
                if calls >= 3:
                    raise ValueError("codex_host_identity_unavailable")
            return physical_host()

        with mock.patch(
            "muninn.review_managed_host.observe", side_effect=observe
        ):
            self.until(
                lambda: self.conn.execute(
                    "SELECT state FROM review_request WHERE id=?", (second.id,)
                ).fetchone()[0]
                == "completed",
                20.0,
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM review_request WHERE id=?",
                (self.request.id,),
            ).fetchone()[0],
            "running",
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            2,
        )
        self.assertEqual(self.marker.read_text().splitlines(), ["0.166.0"])


class CarriedReadyHostTests(ReadyHostTests):
    """Apply the same Ready checks to unchanged legacy request snapshots."""

    carry_legacy = True
