"""Check explicit learning scope and verified history admission."""

from __future__ import annotations

from muninn import review_history, review_state
from muninn.review_progress import source_progress
from tests.test_review_requests import RequestCase


class LearningHistoryTests(RequestCase):
    """Scan disposable original sources without provider calls."""

    def test_preview_does_not_enqueue_and_confirmation_is_required(
        self,
    ) -> None:
        scan = review_history.scan(
            self.conn, self.policy, self.scope.project_id
        )
        self.assertEqual(len(scan.items), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            0,
        )
        with self.assertRaisesRegex(ValueError, "history_scan_changed"):
            self.transaction(
                lambda: review_history.configure(
                    self.conn,
                    self.policy,
                    "all",
                    self.scope.project_id,
                    20.0,
                    "a" * 64,
                )
            )
        result = self.transaction(
            lambda: review_history.configure(
                self.conn,
                self.policy,
                "all",
                self.scope.project_id,
                20.0,
                scan.digest,
            )
        )
        self.assertEqual(result["enqueued"], 1)
        repeated = self.transaction(
            lambda: review_history.configure(
                self.conn,
                self.policy,
                "all",
                self.scope.project_id,
                21.0,
                scan.digest,
            )
        )
        self.assertEqual(repeated["enqueued"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            1,
        )

    def test_changed_original_is_excluded_after_preview(self) -> None:
        preview = review_history.scan(
            self.conn, self.policy, self.scope.project_id
        )
        self.path.write_bytes(
            self.path.read_bytes().replace(b"q one", b"q two")
        )
        with self.assertRaisesRegex(ValueError, "history_scan_changed"):
            self.transaction(
                lambda: review_history.configure(
                    self.conn,
                    self.policy,
                    "all",
                    self.scope.project_id,
                    20.0,
                    preview.digest,
                )
            )
        scan = review_history.scan(
            self.conn, self.policy, self.scope.project_id
        )
        self.assertEqual(len(scan.items), 0)
        self.assertGreater(len(scan.excluded), 0)

    def test_from_now_snapshots_existing_records(self) -> None:
        self.transaction(
            lambda: review_history.configure(
                self.conn, self.policy, "now", None, 20.0, None
            )
        )
        selection = review_state.selection(self.conn)
        assert selection is not None
        self.assertEqual(selection.mode, "now")
        progress = self.conn.execute("SELECT * FROM review_source").fetchone()
        self.assertEqual(progress["activation_line"], 3)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_request"
            ).fetchone()[0],
            0,
        )

    def test_completed_history_is_not_reviewed_again(self) -> None:
        request = self.enqueue()
        self.receipt(request.id)
        scan = review_history.scan(
            self.conn, self.policy, self.scope.project_id
        )
        self.assertEqual(len(scan.items), 0)
        self.assertIsNone(scan.estimate["api_cost_decimal"])

    def test_old_source_indexed_after_selection_stays_excluded(self) -> None:
        self.transaction(
            lambda: review_history.configure(
                self.conn, self.policy, "now", None, 1_800_000_000.0, None
            )
        )
        self.conn.execute("DELETE FROM review_source")
        self.conn.execute("UPDATE source SET first_seen=1800000001.0")
        source = self.conn.execute("SELECT * FROM source").fetchone()
        progress = self.transaction(
            lambda: source_progress(self.conn, source, 1_800_000_002.0)
        )
        self.assertEqual(progress["activation_line"], 3)
