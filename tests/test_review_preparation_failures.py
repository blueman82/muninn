"""Keep source-read refusals distinct from broken review setup."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock

from muninn import review_evidence, review_recovery, review_supervisor
from muninn.review_read_budget import EvidenceLimitError
from tests import test_review_window_boundary as boundary_tests
from tests.ingest_support import rollout
from tests.test_review_supervisor import SupervisorCase


class ReviewPreparationFailureTests(SupervisorCase):
    """Exercise actual bounded reads without launching a provider."""

    def test_large_prefix_is_scanned_once_before_selection(self) -> None:
        self.records[0]["padding"] = "private-source-canary" * 350
        self.write(rollout(), self.records)
        self.run_ingest()
        self.finish()
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "completed",
        )
        self.assertEqual(self.policy.max_input_bytes, 12000)
        self.assertIsNone(review_recovery.notice(self.conn))

    def test_source_read_limit_is_durable_specific_and_not_retried(
        self,
    ) -> None:
        with (
            mock.patch.object(
                review_supervisor.review_prompt,
                "initialize",
                side_effect=ValueError("evidence_limit"),
            ),
            mock.patch.object(self.supervisor, "_create") as create,
        ):
            self.supervisor.tick(13.0)
            self.supervisor.tick(80.0)
        create.assert_not_called()
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["id"], self.request.id)
        self.assertEqual(issue["code"], "evidence_limit")
        self.assertEqual(issue["stage"], "evidence")
        self.assertIn("source verification", issue["cause"])
        self.assertIn("Do not retry", issue["action"])
        self.assertNotIn("private-source-canary", str(issue))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT error_code,outcome FROM review_attempt"
                ).fetchone()
            ),
            ("evidence_limit", "failed"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT state FROM review_request").fetchone()[
                0
            ],
            "failed",
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
        review_recovery.clear(self.conn, self.request.id)
        fallback = review_recovery.details(self.conn, self.request.id)
        self.assertEqual(fallback[0]["code"], "evidence_limit")
        self.assertEqual(fallback[0]["stage"], "not recorded")

    def test_unknown_preparation_error_does_not_expose_text(self) -> None:
        with mock.patch.object(
            review_supervisor.review_prompt,
            "initialize",
            side_effect=ValueError("private-exception-canary"),
        ):
            self.supervisor.tick(13.0)
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["code"], "config")
        self.assertNotIn("private-exception-canary", str(issue))

    def test_typed_preparation_limit_preserves_allowance(self) -> None:
        with (
            mock.patch.object(
                review_supervisor.review_prompt,
                "initialize",
                side_effect=EvidenceLimitError("evidence_work_limit"),
            ),
            mock.patch.object(self.supervisor, "_create") as create,
        ):
            self.supervisor.tick(13.0)
            self.supervisor.tick(80.0)
        create.assert_not_called()
        issue = review_recovery.notice(self.conn)
        assert issue is not None
        self.assertEqual(issue["code"], "evidence_work_limit")
        self.assertEqual(issue["stage"], "evidence")
        self.assertEqual(
            self.conn.execute(
                "SELECT error_code FROM review_attempt"
            ).fetchone()[0],
            "evidence_work_limit",
        )

    def test_fake_provider_waits_for_complete_prompt(self) -> None:
        with subprocess.Popen(
            (sys.executable, "-c", self.script),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            output, errors = child.communicate(b"{}", timeout=5)
        self.assertEqual((child.returncode, errors), (0, b""))
        self.assertEqual(json.loads(output)["type"], "result")


class SelectedWindowBoundaryTests(unittest.TestCase):
    """Retain selected bytes only after complete boundary verification."""

    def setUp(self) -> None:
        self.boundary = boundary_tests.WindowBoundaryTests()
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        self.case = self.boundary.case

    def test_selected_original_shares_the_verified_boundary_scan(self) -> None:
        window, _ = self.boundary.metadata()
        expected = self.case.path.read_bytes().splitlines(keepends=True)[1]
        with mock.patch.object(
            review_evidence, "originals", wraps=review_evidence.originals
        ) as reads:
            number, raw, _ = review_evidence.validate_window_boundary(
                self.case.conn, window, 100_000, selected_line=2
            )
        self.assertEqual((number, raw), (2, expected))
        self.assertEqual(reads.call_count, 1)
        with self.assertRaisesRegex(ValueError, "evidence_changed"):
            review_evidence.validate_window_boundary(
                self.case.conn,
                window.model_copy(update={"boundary_hash": "a" * 64}),
                100_000,
                selected_line=2,
            )

    def test_selected_line_outside_window_never_reads_originals(self) -> None:
        window, _ = self.boundary.metadata()
        for number in (0, window.through_line + 1):
            with (
                self.subTest(line=number),
                mock.patch.object(review_evidence, "originals") as reads,
                self.assertRaisesRegex(ValueError, "rejected_scope"),
            ):
                review_evidence.validate_window_boundary(
                    self.case.conn, window, 100_000, selected_line=number
                )
            reads.assert_not_called()
