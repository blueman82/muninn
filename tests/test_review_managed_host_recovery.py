"""Keep physical preparation refusals in the existing setup recovery path."""

from __future__ import annotations

from unittest import mock

from muninn import review_recovery
from tests.managed_codex_support import physical_host
from tests.test_review_managed_supervisor import ManagedSupervisorTests


class ManagedHostRecoveryTests(ManagedSupervisorTests):
    """Exercise ongoing recovery without a native ID call."""

    policy_revision = 3

    def test_physical_refusal_stops_before_wrapper_and_reservation(
        self,
    ) -> None:
        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                side_effect=ValueError("codex_host_identity_unavailable"),
            ),
            mock.patch(
                "muninn.review_round_launch.create_wrapper",
                side_effect=AssertionError("unexpected wrapper"),
            ),
        ):
            self.supervisor.tick(20.0)
        notice = review_recovery.notice(self.conn)
        assert notice is not None
        self.assertEqual(notice["code"], "codex_host_identity_unavailable")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )
        self.assertFalse(self.marker.exists())

    def test_reused_execution_refuses_without_a_second_reservation(
        self,
    ) -> None:
        with (
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                side_effect=AssertionError("legacy host getter"),
            ),
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                side_effect=AssertionError("legacy launch getter"),
            ),
            mock.patch(
                "muninn.review_managed_host.observe",
                side_effect=[
                    physical_host(),
                    ValueError("codex_host_identity_unavailable"),
                ],
            ),
            mock.patch(
                "muninn.review_round_launch.create_wrapper",
                side_effect=AssertionError("unexpected wrapper"),
            ),
        ):
            self.supervisor.tick(20.0)
        before = tuple(
            self.conn.execute("SELECT * FROM review_call").fetchone()
        )
        notice = review_recovery.notice(self.conn)
        assert notice is not None
        self.assertEqual(notice["code"], "codex_host_identity_unavailable")
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_call").fetchone()[0],
            "reserved",
        )
        with (
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=physical_host("7" * 64),
            ),
            mock.patch(
                "muninn.review_round_launch.create_wrapper",
                side_effect=AssertionError("unexpected wrapper"),
            ),
        ):
            self.supervisor.tick(81.0)
        notice = review_recovery.notice(self.conn)
        assert notice is not None
        self.assertEqual(notice["code"], "codex_host_changed")
        self.assertEqual(
            tuple(self.conn.execute("SELECT * FROM review_call").fetchone()),
            before,
        )
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
        self.assertFalse(self.marker.exists())
