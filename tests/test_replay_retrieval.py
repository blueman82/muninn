"""The retrieval replay tool on a synthetic store with known gold ranks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.query_support import OpenCase, sha
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
        return self.run_file(path, *flags)

    def run_file(
        self, path: Path, *flags: str
    ) -> tuple[int, dict[str, Any], str]:
        """Run the tool on a file; return code, report, stderr."""
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
        self.assertFalse(any(q["no_terms"] for q in report["questions"]))
        self.assertEqual(ghost["refs"][0]["rank"], None)
        self.assertFalse(ghost["refs"][0]["resolves"])
        self.assertEqual(ctl["results"], 0)
        self.assertEqual(ctl2["results"], 3)
        self.assertEqual((ctl["final_hits"], ctl2["final_hits"]), (0, 2))
        summary = report["summary"]
        self.assertGreater(summary.pop("final_bytes_mean"), 0)
        self.assertEqual(
            summary,
            {
                "questions_with_final_hits": 2,
                "distinct_sessions_mean": 2.0,
                "max_session_share_mean": 0.5,
                "candidate_recall": 0.75,
                "complete_required_set_at_final": 0.0,
                "citation_resolves_rate": 0.75,
                "controls_passed": 1,
                "controls_total": 2,
                "unresolved_refs": 1,
                "no_terms_questions": 0,
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

    def test_kind_filters_candidates_and_final_hits(self) -> None:
        self.write_source("owner", [(f"{CANARY} " + "padding " * 10, {})])
        self.write_source(
            "assistant",
            [(CANARY, {"kind": "reply", "role": "assistant"})],
        )
        questions = [
            {"id": "g", "query": CANARY, "required": ["codex:owner:1.1"]}
        ]
        reports = [self.replay(questions, "--final", "1")[1]]
        for kind in ("prompt", "reply"):
            code, report, err = self.replay(
                questions, "--final", "1", "--kind", kind
            )
            self.assertEqual((code, err, report["kind"]), (0, "", kind))
            reports.append(report)
        gold = [report["questions"][0] for report in reports]
        self.assertIsNone(reports[0]["kind"])
        self.assertEqual(
            [q["all_in_final"] for q in gold], [False, True, False]
        )
        self.assertEqual([q["refs"][0]["rank"] for q in gold], [2, 1, None])
        self.assertTrue(all(q["refs"][0]["resolves"] for q in gold))

    def test_kind_rejects_non_conversational_events(self) -> None:
        with self.assertRaises(SystemExit) as bad:
            self.replay([], "--kind", "tool_call")
        self.assertEqual(bad.exception.code, 2)

    def test_malformed_input_fails_closed(self) -> None:
        bad: list[Any] = [
            {"id": "a", "query": CANARY, "required": []},
            {"id": "a", "query": CANARY, "required": ["x"], "control": True},
            {"id": 1, "query": CANARY, "required": ["x"]},
            {"id": "a", "query": CANARY, "required": ["x"], "extra": 1},
            {"id": "a", "query": CANARY, "required": ["x", "x"]},
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

    def test_duplicate_ids_alone_fail_closed(self) -> None:
        one = {"id": "a", "query": CANARY, "required": ["x"]}
        code, report, _ = self.replay([one, dict(one)])
        self.assertEqual((code, report), (2, {}))

    def test_other_malformed_documents_fail_closed(self) -> None:
        path = self.tmp / "q.json"
        for raw in (
            b'{"other": []}',
            b'{"questions": []}',
            b'[{"id": "a"}]',
            b'{"questions": [{"id": "\xff"}]}',
        ):
            path.write_bytes(raw)
            code, report, err = self.run_file(path)
            self.assertEqual((code, report), (2, {}))
            self.assertEqual(err.strip(), "invalid questions file")

    def test_bad_options_exit_2(self) -> None:
        question = [{"id": "g", "query": "zephyr", "required": self.refs}]
        for flags in (
            ["--final", "0"],
            ["--final", "31"],
            ["--candidates", "0"],
            ["--per-session", "0"],
        ):
            code, report, err = self.replay(question, *flags)
            self.assertEqual((code, report), (2, {}))
            self.assertEqual(err.strip(), "invalid option: " + flags[0][2:])

    def test_missing_store_exits_4_without_text(self) -> None:
        question = [{"id": "g", "query": CANARY, "required": ["x"]}]
        empty = self.tmp / "nohome"
        empty.mkdir()
        code, report, err = self.replay(question, "--home", str(empty))
        self.assertEqual((code, report), (4, {}))
        self.assertEqual(err.strip(), "store unavailable")

    def test_search_error_and_sqlite_error_exit_cleanly(self) -> None:
        question = [{"id": "g", "query": "zephyr", "required": self.refs}]
        with patch.object(replay_retrieval.query, "search") as search:
            search.return_value = {"error": "bad_kind"}
            code, report, err = self.replay(question)
        self.assertEqual(
            (code, report, err.strip()), (2, {}, "invalid option: bad_kind")
        )
        with patch.object(
            replay_retrieval.store, "connect_ro", side_effect=sqlite3.Error
        ):
            code, report, err = self.replay(question)
        self.assertEqual(
            (code, report, err.strip()), (4, {}, "store unavailable")
        )

    def test_no_terms_control_is_not_passed_and_gold_is_flagged(
        self,
    ) -> None:
        code, report, _ = self.replay(
            [
                {"id": "ctl", "query": "the of", "control": True},
                {"id": "g", "query": "the of", "required": self.refs[:1]},
            ]
        )
        self.assertEqual(code, 0)
        ctl, gold = report["questions"]
        self.assertTrue(ctl["no_terms"])
        self.assertTrue(gold["no_terms"])
        summary = report["summary"]
        self.assertEqual(summary["controls_passed"], 0)
        self.assertEqual(summary["controls_total"], 1)
        self.assertEqual(summary["no_terms_questions"], 2)

    def test_terms_rule_changes_results_and_reports_terms_kept(self) -> None:
        self.write_source("thr-stop", [("the of and more", {})])
        questions = [
            {"id": "ctl", "query": "the of", "control": True},
            {"id": "g", "query": "the zephyr", "required": self.refs[:1]},
        ]
        _, listed, _ = self.replay(questions)
        _, none, _ = self.replay(questions, "--terms", "none")
        _, df, _ = self.replay(questions, "--terms", "df:0.5")
        self.assertEqual(
            [(q["no_terms"], q["results"]) for q in listed["questions"][:1]],
            [(True, 0)],
        )
        self.assertGreater(none["questions"][0]["results"], 0)
        self.assertFalse(none["questions"][0]["no_terms"])
        self.assertEqual(
            [q["terms_kept"] for q in listed["questions"]], [0, 1]
        )
        self.assertEqual([q["terms_kept"] for q in none["questions"]], [2, 2])
        # A store under MIN_EVENTS: df behaves as the list rule.
        self.assertEqual(df["questions"], listed["questions"])

    def test_bad_terms_exit_2(self) -> None:
        question = [{"id": "g", "query": "zephyr", "required": self.refs}]
        for value in ("", "x", "df:0", "df:2", "df:"):
            code, report, err = self.replay(question, "--terms", value)
            self.assertEqual((code, report), (2, {}))
            self.assertEqual(err.strip(), "invalid option: terms")

    def test_unresolved_ref_is_counted(self) -> None:
        _, report, _ = self.replay(
            [{"id": "g", "query": "zephyr", "required": ["codex:no:9.1"]}]
        )
        self.assertEqual(report["summary"]["unresolved_refs"], 1)
        self.assertEqual(report["summary"]["citation_resolves_rate"], 0.0)

    def cited_event(self, thread: str, scope: int, text: str, cwd: str) -> int:
        """Add an event whose transcript line exists, in any scope."""
        raw = json.dumps({"n": 1, "t": text}).encode()
        (self.sessions / f"{thread}.jsonl").write_bytes(raw + b"\n")
        return self.add_event(
            self.add_source(thread), scope, text, cwd=cwd, digest=sha(raw)
        )

    def test_scope_flags_change_ranks_and_default_is_all_projects(
        self,
    ) -> None:
        far = self.add_scope("/far")
        outside = self.cited_event("thr-far", far, "zephyr " * 4, "/far")
        near = self.cited_event(
            "thr-near", self.repo, "zephyr zephyr", "/repo"
        )
        question = [
            {
                "id": "g",
                "query": "zephyr",
                "required": [str(outside), str(near)],
            }
        ]
        _, report, _ = self.replay(question)
        self.assertEqual(report["questions"][0]["refs"][0]["rank"], 1)
        self.assertTrue(report["questions"][0]["refs"][0]["resolves"])
        for flags in (
            ["--no-all-projects", "--cwd", "/repo"],
            ["--scope", "/repo"],
        ):
            _, report, _ = self.replay(question, *flags, "--final", "5")
            ref, inside = report["questions"][0]["refs"]
            self.assertEqual((ref["rank"], ref["in_final"]), (None, False))
            self.assertTrue(ref["resolves"] and inside["resolves"])
            self.assertIsNotNone(inside["rank"])

    def final_flags(
        self, texts: list[str], query: str, *flags: str
    ) -> list[bool]:
        """Put the texts in one new session; return each one's in_final."""
        source = self.add_source("thr-" + "".join(flags))
        ids = [self.add_event(source, self.repo, text) for text in texts]
        question = [
            {"id": "g", "query": query, "required": [str(i) for i in ids]}
        ]
        _, report, _ = self.replay(question, "--final", "10", *flags)
        refs = report["questions"][0]["refs"]
        self.assertTrue(all(r["rank"] for r in refs))  # all candidates
        return [r["in_final"] for r in refs]

    def test_in_final_follows_repeat_collapse(self) -> None:
        # Identical text in one session collapses into the first hit.
        got = self.final_flags(["quark one", "quark one"], "quark")
        self.assertEqual(got, [True, False])

    def test_in_final_follows_per_session_cap(self) -> None:
        # At most two hits per session: the worst-ranked of three drops.
        texts = ["quark quark quark", "quark quark pad", "quark pad pad pad"]
        self.assertEqual(self.final_flags(texts, "quark"), [True, True, False])

    def test_per_session_flag_moves_the_final_cut(self) -> None:
        texts = ["quark quark quark", "quark quark pad", "quark pad pad pad"]
        for cap, want in (
            ("1", [True, False, False]),
            ("2", [True, True, False]),
            ("3", [True, True, True]),
        ):
            with self.subTest(cap=cap):
                got = self.final_flags(texts, "quark", "--per-session", cap)
                self.assertEqual(got, want)

    def test_diversity_metrics_follow_the_cap(self) -> None:
        source = self.add_source("thr-one")
        for text in ("quark quark quark", "quark quark pad", "quark pad pad"):
            self.add_event(source, self.repo, text)
        question = [{"id": "g", "query": "quark", "required": ["codex:x:1.1"]}]
        sizes = []
        for cap, hits in (("2", 2), ("3", 3)):
            _, report, _ = self.replay(question, "--per-session", cap)
            got = report["questions"][0]
            self.assertEqual(got["final_hits"], hits)
            self.assertEqual(got["distinct_sessions"], 1)
            self.assertEqual(got["max_session_share"], 1.0)
            sizes.append(got["final_bytes"])
        self.assertLess(sizes[0], sizes[1])
        _, report, _ = self.replay(
            [{"id": "z", "query": "zephyr", "required": self.refs}]
        )
        got = report["questions"][0]
        self.assertEqual(got["distinct_sessions"], 3)
        self.assertEqual(got["max_session_share"], 0.3333)

    def test_bad_per_session_exits_2(self) -> None:
        question = [{"id": "g", "query": "zephyr", "required": self.refs}]
        for value in ("0", "-1"):
            with self.subTest(value=value):
                code, report, _ = self.replay(question, "--per-session", value)
                self.assertEqual((code, report), (2, {}))

    def test_means_share_one_denominator(self) -> None:
        _, report, _ = self.replay(
            [
                {"id": "full", "query": "zephyr", "required": self.refs},
                {"id": "empty", "query": CANARY, "required": ["x"]},
            ]
        )
        empty = report["questions"][1]
        self.assertEqual(empty["final_hits"], 0)
        self.assertEqual(empty["distinct_sessions"], 0)
        self.assertIsNone(empty["max_session_share"])
        summary = report["summary"]
        self.assertEqual(summary["questions_with_final_hits"], 1)
        self.assertEqual(summary["distinct_sessions_mean"], 3.0)
        self.assertEqual(summary["max_session_share_mean"], 0.3333)

    def test_means_are_none_without_final_hits(self) -> None:
        for questions in (
            [{"id": "c", "query": "the of", "control": True}],
            [{"id": "e", "query": CANARY, "required": ["x"]}],
        ):
            code, report, _ = self.replay(questions)
            self.assertEqual(code, 0)
            summary = report["summary"]
            self.assertEqual(summary["questions_with_final_hits"], 0)
            for key in (
                "distinct_sessions_mean",
                "max_session_share_mean",
                "final_bytes_mean",
            ):
                self.assertIsNone(summary[key])
        self.assertEqual(report["questions"][0]["final_hits"], 0)

    def test_per_session_must_be_an_integer_and_huge_means_uncapped(
        self,
    ) -> None:
        question = [{"id": "g", "query": "zephyr", "required": self.refs}]
        with self.assertRaises(SystemExit) as stop:
            self.replay(question, "--per-session", "abc")
        self.assertEqual(stop.exception.code, 2)
        _, huge, _ = self.replay(question, "--per-session", str(10**9))
        _, big, _ = self.replay(question, "--per-session", "100")
        for report in (huge, big):
            report.pop("per_session")
        self.assertEqual(huge, big)
