"""Build decisions from disposable, physically backed admitted originals."""

from __future__ import annotations

from muninn.decision_candidates import stage_candidate
from muninn.decision_models import CandidateInput, DecisionView
from muninn.knowledge_models import ReviewResult
from tests.test_review_commit import CommitCase


class DecisionCase(CommitCase):
    """Reuse the real review preparation without granting owner decisions."""

    def stage(self, result: ReviewResult | None = None) -> DecisionView:
        """Stage inside a caller transaction and leave its ownership intact."""
        return self.transaction(
            lambda: stage_candidate(
                self.conn,
                self.home,
                CandidateInput(
                    request_id=self.context.request_id,
                    result=result or self.result([self.note()]),
                    disposition=None,
                ),
                14.0,
            )
        )

    def projection(self) -> dict[str, list[tuple[object, ...]]]:
        """Read every table, including FTS shadows, for mutation checks."""
        return {
            row[0]: [
                tuple(item)
                for item in self.conn.execute(f'SELECT * FROM "{row[0]}"')
            ]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
