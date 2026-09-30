"""§6 metrics and §7 analysis/report on synthetic graded units."""

from __future__ import annotations

import hashlib
import json
import unittest

from trial_harness import metrics, report

REQ = {
    "provider": "codex",
    "source_path": "a",
    "line": 5,
    "record_sha256": "r",
}
OTHER = {
    "provider": "codex",
    "source_path": "b",
    "line": 7,
    "record_sha256": "o",
}


def ident_key(identity: dict) -> tuple:
    return metrics.identity_key(identity)


def call(seq, command, returned, t0=0, t1=100, error=None):
    return {
        "seq": seq,
        "argv": [command],
        "t_start": t0,
        "t_end": t1,
        "bytes_out": 10,
        "returned_ids": returned,
        "error": error,
    }


def ret(identity, kind, chars=None):
    return {"identity": identity, "class": kind, "chars": chars}


def good_cite(**overrides) -> dict:
    item = {
        "identity": REQ,
        "identity_valid": True,
        "opened": True,
        "in_scope": True,
        "role_ok": True,
        "origin": "ORIGINAL",
    }
    item.update(overrides)
    return item


def judgement(present=True, support="FIRST_HAND", additions=(), nonconv=False):
    return {
        "claims": [
            {
                "claim": 1,
                "present": present,
                "contradicted": False,
                "support": [{"citation": 1, "type": support}],
            }
        ],
        "additions": [
            {"text": "x", "support": [{"citation": 1, "type": t}]}
            for t in additions
        ],
        "citations": [{"citation": 1, "non_conversational": nonconv}],
    }


class LogTest(unittest.TestCase):
    TEXT = {ident_key(REQ): 1000, ident_key(OTHER): 80}

    def test_surfaced_and_opened_sets(self) -> None:
        log = [
            call(1, "files", [ret(OTHER, "listing")]),
            call(2, "events", [ret(REQ, "listing")]),
            call(3, "open", [ret(REQ, "open", [0, 480])]),
            call(4, "open", [ret(OTHER, "open", [0, 80])]),
        ]
        self.assertEqual(
            metrics.surfaced(log), {ident_key(REQ), ident_key(OTHER)}
        )
        self.assertEqual(metrics.opened(log, self.TEXT), {ident_key(OTHER)})
        log.append(call(5, "open", [ret(REQ, "open", [480, 960])]))
        self.assertIn(ident_key(REQ), metrics.opened(log, self.TEXT))

    def test_cost_counts_classes_bytes_latency_and_ceiling(self) -> None:
        log = [
            call(1, "files", [], 0, 50),
            call(2, "open", [], 60, 160),
            call(3, "open", [], 200, 300, error="call limit reached"),
        ]
        cost = metrics.cost(log, {"files": "listing", "open": "open"})
        self.assertEqual(cost["calls"], 3)
        self.assertEqual(cost["by_command"], {"files": 1, "open": 2})
        self.assertEqual((cost["listing_calls"], cost["open_calls"]), (1, 2))
        self.assertEqual(cost["bytes"], 30)
        self.assertEqual(cost["wall_ms"], 300)
        self.assertEqual(cost["median_latency_ms"], 100)
        self.assertTrue(cost["ceiling_hit"])
        self.assertEqual(cost["reader_tokens"], "NOT_RECORDED")

    def test_listing_budget_of_first_open(self) -> None:
        log = [call(n, "events", [ret(OTHER, "listing")]) for n in range(16)]
        log.append(call(16, "open", [ret(REQ, "open", [0, 900])]))
        classes = {"events": "listing", "open": "open"}
        first = metrics.first_open_listing_count(log, self.TEXT, classes)
        self.assertEqual(first[ident_key(REQ)], 16)


class ChainTest(unittest.TestCase):
    def test_hash_chain_verifies_and_detects_tampering(self) -> None:
        lines, prev = [], "0" * 64
        for seq in range(3):
            line = json.dumps({"seq": seq, "prev_sha256": prev})
            lines.append(line)
            prev = hashlib.sha256(line.encode()).hexdigest()
        self.assertTrue(metrics.chain_ok(lines))
        lines[1] = lines[1].replace('"seq": 1', '"seq": 9')
        self.assertFalse(metrics.chain_ok(lines))


class StageTest(unittest.TestCase):
    def stage(self, cites, judged, surfaced, opened, reachable=True):
        return metrics.stage(
            required=[ident_key(REQ)],
            cites=cites,
            judgement=judged,
            surfaced=surfaced,
            opened=opened,
            reachable={ident_key(REQ): reachable},
        )

    def test_unsurfaced_required_is_split_by_reachability(self) -> None:
        cites, judged = [good_cite()], judgement(present=False)
        self.assertEqual(
            self.stage(cites, judged, set(), set(), reachable=False),
            "NOT_SURFACED:NOT_REACHABLE",
        )
        self.assertEqual(
            self.stage(cites, judged, set(), set()),
            "NOT_SURFACED:REACHABLE_NOT_SURFACED",
        )

    def test_contamination_precedes_opening_and_wrong_answer(self) -> None:
        req = {ident_key(REQ)}
        self.assertEqual(
            self.stage(
                [good_cite(origin="REPLAY_COPY")], judgement(), req, set()
            ),
            "CONTAMINATION_CITED",
        )
        self.assertEqual(
            self.stage(
                [good_cite()], judgement(support="SECOND_HAND"), req, req
            ),
            "CONTAMINATION_CITED",
        )

    def test_surfaced_not_opened_then_opened_but_wrong(self) -> None:
        req = {ident_key(REQ)}
        self.assertEqual(
            self.stage([good_cite()], judgement(present=False), req, set()),
            "SURFACED_NOT_OPENED",
        )
        self.assertEqual(
            self.stage([good_cite()], judgement(present=False), req, req),
            "OPENED_BUT_ANSWER_WRONG",
        )


class CriteriaTest(unittest.TestCase):
    def test_labels_accumulate(self) -> None:
        labels = metrics.failing_criteria(
            cites=[good_cite(in_scope=False, origin="INJECTED")],
            judgement=judgement(present=False, additions=["NONE"]),
            abstained=False,
            format_fail=False,
        )
        self.assertEqual(
            labels,
            [
                "MISSING_CLAIM",
                "UNSUPPORTED_ADDITION",
                "NON_ORIGINAL_SUPPORT",
                "OUT_OF_SCOPE",
            ],
        )

    def test_abstention_and_format_fail(self) -> None:
        self.assertEqual(
            metrics.failing_criteria([], None, True, False), ["ABSTAINED"]
        )
        self.assertEqual(
            metrics.failing_criteria([], None, False, True), ["FORMAT_FAIL"]
        )


class FunnelTest(unittest.TestCase):
    def test_funnel_counts_each_stage(self) -> None:
        cites = [
            good_cite(),
            good_cite(origin="REPLAY_COPY"),
            good_cite(identity_valid=False),
        ]
        judged = judgement()
        judged["citations"] = [
            {"citation": n, "non_conversational": False} for n in (1, 2, 3)
        ]
        self.assertEqual(
            metrics.funnel(cites, judged),
            {
                "citations": 3,
                "identity_valid": 2,
                "valid_P": 2,
                "valid_X": 1,
                "first_hand": 1,
            },
        )


def unit(arm, qid, x, **extra) -> dict:
    base = {
        "unit_id": f"{arm}:{qid}",
        "arm": arm,
        "qid": qid,
        "kind": "ANSWER",
        "abstained": False,
        "format_fail": False,
        "citations": [good_cite()],
        "judgement": judgement(),
        "final": {"P": True, "R": True, "X": x},
        "required": [ident_key(REQ)],
        "reachable": {ident_key(REQ): True},
        "surfaced": {ident_key(REQ)},
        "opened": {ident_key(REQ)},
        "first_open_listings": {ident_key(REQ): 3},
        "cost": None,
        "scope_leak": 0,
    }
    base.update(extra)
    return base


class SummaryTest(unittest.TestCase):
    def test_unit_summary_attributes_failed_answers(self) -> None:
        passed = metrics.unit_summary(unit("NEW", "E1", True))
        self.assertTrue(passed["sensitivity_15"])
        self.assertIsNone(passed["stage"])
        failed = metrics.unit_summary(
            unit(
                "NEW",
                "E2",
                False,
                opened=set(),
                judgement=judgement(present=False),
            )
        )
        self.assertEqual(failed["stage"], "SURFACED_NOT_OPENED")
        self.assertEqual(failed["criteria"], ["MISSING_CLAIM"])
        self.assertEqual(failed["candidate_recall"], 1.0)
        self.assertEqual(failed["opened_recall"], 0.0)

    def test_arm_summary_counts_primary_controls_and_abstentions(self) -> None:
        units = [
            unit("NEW", "E1", True),
            unit(
                "NEW",
                "E2",
                False,
                abstained=True,
                final={"P": False, "R": False, "X": False},
            ),
            dict(
                unit("NEW", "W1", False),
                kind="W",
                final={"CONTROL": True},
                control_label="CAN_FAIL",
                required=[],
            ),
            dict(
                unit("NEW", "E1:MIRROR", False),
                kind="MIRROR",
                final={"CONTROL": False},
                required=[],
                scope_leak=2,
            ),
        ]
        summary = metrics.arm_summary(units)["NEW"]
        self.assertEqual(summary["answerable"], 2)
        self.assertEqual(summary["X_pass"], 1)
        self.assertEqual(summary["over_abstention"], 1)
        self.assertEqual(
            summary["W"], {"pass": 1, "units": 1, "labels": {"CAN_FAIL": 1}}
        )
        self.assertEqual(summary["MIRROR"], {"pass": 0, "units": 1})
        self.assertEqual(summary["scope_leak"], 2)
        self.assertEqual(summary["reachable_all_required"], 2)
        self.assertEqual(summary["stages"], {"OPENED_BUT_ANSWER_WRONG": 1})
        self.assertEqual(summary["criteria"], {"ABSTAINED": 1})


class McNemarTest(unittest.TestCase):
    def test_protocol_table_values(self) -> None:
        table = {
            (5, 0): 0.0625,
            (6, 0): 0.031,
            (7, 1): 0.070,
            (8, 1): 0.039,
            (9, 2): 0.065,
            (10, 2): 0.039,
        }
        for (c, b), p in table.items():
            self.assertAlmostEqual(report.mcnemar_exact(b, c), p, places=3)

    def test_no_discordant_pairs_gives_one(self) -> None:
        self.assertEqual(report.mcnemar_exact(0, 0), 1.0)

    def test_interpretation_needs_six_net_flips_and_p(self) -> None:
        self.assertEqual(report.interpret(0, 6), "NEW_BETTER_DEV")
        self.assertEqual(report.interpret(6, 0), "NEW_WORSE_DEV")
        self.assertEqual(report.interpret(0, 5), "NO_DETECTABLE_DIFFERENCE")
        self.assertEqual(report.interpret(2, 9), "NO_DETECTABLE_DIFFERENCE")


class PrimaryTest(unittest.TestCase):
    def test_pairs_exclude_drift_and_fatal_questions(self) -> None:
        old = {"E1": True, "E2": False, "E3": True, "E4": False}
        new = {"E1": False, "E2": True, "E3": True, "E4": True}
        result = report.primary(
            old, new, exclusions={"E4": "CORPUS_DRIFT"}, fatal_units=0
        )
        self.assertEqual((result["b"], result["c"]), (1, 1))
        self.assertEqual(result["valid_pairs"], 3)
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertEqual(result["flips"], {"E1": "OLD_ONLY", "E2": "NEW_ONLY"})

    def test_more_than_two_fatal_units_voids(self) -> None:
        result = report.primary({}, {}, exclusions={}, fatal_units=3)
        self.assertEqual(result["status"], "VOID")


class HeadlineTest(unittest.TestCase):
    def test_headline_always_carries_evidence_labels(self) -> None:
        text = report.render(
            {
                "primary": report.primary(
                    {f"Q{i}": True for i in range(30)},
                    {f"Q{i}": True for i in range(30)},
                    exclusions={},
                    fatal_units=0,
                ),
                "kappa_x": 0.5,
                "safety": ["NEW W3 failed"],
                "replication": {"b": 0, "c": 7, "p": 0.0156},
            }
        )
        head = text.split("\n\n")[0]
        self.assertIn("DEVELOPMENT EVIDENCE", head)
        self.assertIn("confirmation NOT_RUN", head)
        self.assertIn("LOW_GRADER_AGREEMENT", head)
        self.assertIn("NEW W3 failed", head)
        self.assertIn("round-four baseline did not replicate", head)
        separators = [
            line for line in text.splitlines() if line.startswith("|---")
        ]
        self.assertGreaterEqual(len(separators), 2)
        self.assertEqual(text.count(report.TABLE_LABEL), len(separators))


if __name__ == "__main__":
    unittest.main()
