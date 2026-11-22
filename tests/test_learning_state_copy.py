"""Keep learning choices and repair notes during store replacement."""

from __future__ import annotations

from muninn import store_copy
from tests.test_review_requests import RequestCase


class LearningStateCopyTests(RequestCase):
    """Check both older stores and stores with additive learning state."""

    def test_unknown_extension_shape_is_refused(self) -> None:
        self.conn.execute(
            "ALTER TABLE learning_setup ADD COLUMN surprise TEXT"
        )
        with self.assertRaisesRegex(ValueError, "cutover_source_mismatch"):
            store_copy.validate_source(self.conn)
