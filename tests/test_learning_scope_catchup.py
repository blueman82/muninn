"""Keep late-import scope choices compatible with bounded catch-up."""

from __future__ import annotations

from unittest import mock

from muninn import hook_learn, review_catchup, review_state
from tests.test_review_requests import RequestCase


class ScopeCatchupTests(RequestCase):
    """Use original source verification for both saved learning choices."""

    def select(self, mode: str) -> None:
        """Save a choice after this fixture's original transcript date."""
        self.conn.execute(
            "UPDATE review_control SET activated_at=1800000000.0"
        )
        self.conn.execute("UPDATE source SET first_seen=1800000001.0")
        self.conn.execute("BEGIN IMMEDIATE")
        review_state.ensure(self.conn)
        self.conn.execute(
            "INSERT INTO learning_setup VALUES(1,?,NULL,1800000000.0,NULL)",
            (mode,),
        )
        self.conn.commit()

    def catch_up(self) -> None:
        """Run one bounded caller-owned catch-up transaction."""
        self.transaction(
            lambda: review_catchup.catch_up(
                self.conn, self.policy, 1800000002.0, 0
            )
        )

    def count(self) -> int:
        """Count requests without equating admission with completion."""
        return self.conn.execute(
            "SELECT count(*) FROM review_request"
        ).fetchone()[0]

    def test_all_includes_verified_older_late_import(self) -> None:
        self.select("all")
        self.catch_up()
        self.assertEqual(self.count(), 1)

    def test_now_excludes_older_late_import(self) -> None:
        self.select("now")
        self.catch_up()
        self.assertEqual(self.count(), 0)

    def test_all_still_requires_native_source_verification(self) -> None:
        self.select("all")
        with mock.patch.object(
            hook_learn,
            "validated_source",
            side_effect=ValueError("source_unavailable"),
        ):
            self.catch_up()
        self.assertEqual(self.count(), 0)

    def test_project_history_does_not_admit_other_project_history(
        self,
    ) -> None:
        self.select("all")
        self.conn.execute(
            "UPDATE learning_setup SET project_id='other-project'"
        )
        self.catch_up()
        self.assertEqual(self.count(), 0)

    def test_project_history_includes_its_verified_late_import(self) -> None:
        self.select("all")
        self.conn.execute(
            "UPDATE learning_setup SET project_id=?", (self.scope.project_id,)
        )
        self.catch_up()
        self.assertEqual(self.count(), 1)
