"""The retrieval replay tool on a synthetic store with known gold ranks."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.query_support import OpenCase
from tools import replay_retrieval

CANARY = "canaryneedle"


class ReplayTests(OpenCase):
    """Ranks, final cut, citations and controls, with nothing written."""

    refs: list[str]

    def setUp(self) -> None:
        super().setUp()
        texts = [
            "zephyr zephyr zephyr",
            "zephyr with some extra filler words to dilute the score here",
            "zephyr " + "padding " * 20,
        ]
        self.refs = []
        for index, text in enumerate(texts):
            self.write_source(f"thr-{index}", [(text, {})])
            self.refs.append(f"codex:thr-{index}:1.1")
        self.noise(self.repo)

    def replay(
        self, questions: list[dict[str, Any]], *flags: str
    ) -> tuple[int, dict[str, Any], str]:
        """Run the tool on a questions file; return code, report, stderr."""
        path = self.tmp / "q.json"
        path.write_text(json.dumps({"questions": questions}))
        out, err = io.StringIO(), io.StringIO()
        roots = json.dumps({"codex-sessions": str(self.sessions)})
        with (
            patch.dict(os.environ, {"MUNINN_ROOTS": roots}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = replay_retrieval.main(
                [str(path), "--home", str(self.home), *flags]
            )
        return code, json.loads(out.getvalue() or "{}"), err.getvalue()

    def test_ranks_final_citations_and_controls(self) -> None:
        before = sorted(p.name for p in self.home.iterdir())
        code, report, _ = self.replay(
            [
                {"id": "gold", "query": "zephyr", "required": self.refs},
                {
                    "id": "ghost",
                    "query": "zephyr",
                    "required": ["codex:no:9.1"],
                },
                {"id": "ctl", "query": CANARY, "control": True},
                {"id": "ctl2", "query": "zephyr", "control": True},
            ],
            "--final",
            "2",
        )
        self.assertEqual(code, 0)
        gold, ghost, ctl, ctl2 = report["questions"]
        self.assertEqual([r["rank"] for r in gold["refs"]], [1, 2, 3])
        self.assertEqual(
            [r["in_final"] for r in gold["refs"]], [True, True, False]
        )
        self.assertTrue(gold["all_in_candidates"])
        self.assertFalse(gold["all_in_final"])
        self.assertTrue(all(r["resolves"] for r in gold["refs"]))
        self.assertEqual(ghost["refs"][0]["rank"], None)
        self.assertFalse(ghost["refs"][0]["resolves"])
        self.assertEqual(ctl["results"], 0)
        self.assertEqual(ctl2["results"], 3)
        self.assertEqual(
            report["summary"],
            {
                "candidate_recall": 0.75,
                "complete_required_set_at_final": 0.0,
                "citation_resolves_rate": 0.75,
                "controls_passed": 1,
                "controls_total": 2,
            },
        )
        self.assertNotIn(CANARY, json.dumps(report))
        self.assertEqual(before, sorted(p.name for p in self.home.iterdir()))

    def test_candidate_cutoff_limits_rank(self) -> None:
        _, report, _ = self.replay(
            [{"id": "g", "query": "zephyr", "required": self.refs}],
            "--candidates",
            "2",
        )
        refs = report["questions"][0]["refs"]
        self.assertEqual([r["rank"] for r in refs], [1, 2, None])

    def test_malformed_input_fails_closed(self) -> None:
        bad: list[Any] = [
            {"id": "a", "query": CANARY, "required": []},
            {"id": "a", "query": CANARY, "required": ["x"], "control": True},
            {"id": 1, "query": CANARY, "required": ["x"]},
            {"id": "a", "query": CANARY, "required": ["x"], "extra": 1},
        ]
        for question in bad:
            code, report, err = self.replay([question])
            self.assertEqual((code, report), (2, {}))
            self.assertNotIn(CANARY, err)
        path = self.tmp / "q.json"
        path.write_text(f"{{{CANARY}")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(replay_retrieval.main([str(path)]), 2)
        self.assertNotIn(CANARY, err.getvalue())
        self.assertEqual(Path(path).read_text(), f"{{{CANARY}")
