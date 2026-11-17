"""Round two of a corrected review depends on the source, not its length."""

from __future__ import annotations

import os
from unittest import mock, skipIf

from muninn import review_supervisor_result
from muninn.review_models import ReviewContext
from muninn.review_round_state import Round
from tests import test_review_managed_supervisor as managed


@skipIf(os.name == "nt", "Darwin managed route uses POSIX test stand-ins")
class RoundTwoSourceTests(managed.ManagedSupervisorTests):
    """Change the source between a reduction request and round two."""

    def run_between_rounds(self, change: str) -> None:
        """Run the request while `change` happens before round two."""
        self.install("0.166.0", bodies=managed.REDUCTION_BODIES)
        original = review_supervisor_result._advance

        def advance(pending: Round, context: ReviewContext) -> None:
            original(pending, context)
            if change == "grow":
                self.append(self.path, [self.records[-1]])
                self.run_ingest()
            else:
                self.conn.execute("UPDATE source SET status='missing'")

        with mock.patch(
            "muninn.review_supervisor_result._advance", side_effect=advance
        ):
            self.finish()

    def test_live_transcript_growth_keeps_round_two_working(self) -> None:
        self.run_between_rounds("grow")
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "completed",
        )

    def test_source_loss_records_its_own_cause(self) -> None:
        self.run_between_rounds("lose")
        self.assertEqual(
            self.conn.execute(
                "SELECT error_code FROM review_attempt"
            ).fetchone()[0],
            "source_unavailable",
        )
