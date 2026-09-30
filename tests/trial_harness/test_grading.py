"""Grading pipeline: blinding, packets, rules, adjudication, kappa."""

from __future__ import annotations

import hashlib
import json
import random
import unittest

from trial_harness import grading

H = "0123456789abcdef" * 4


class BlindingTest(unittest.TestCase):
    def test_answer_id_is_sha256_of_h16_32_and_unit_id(self) -> None:
        expected = hashlib.sha256((H[16:32] + "HIST:E1").encode()).hexdigest()[
            :10
        ]
        self.assertEqual(grading.answer_id(H, "HIST:E1"), expected)

    def test_grading_order_is_sorted_ids_shuffled_with_h16_32(self) -> None:
        ids = ["b", "a", "c", "d"]
        expected = sorted(ids)
        random.Random(int(H[16:32], 16)).shuffle(expected)
        self.assertEqual(grading.grading_order(ids, H), expected)

    def test_ab_order_is_deterministic_per_answer(self) -> None:
        first = grading.ab_order(H, "abc")
        self.assertEqual(first, grading.ab_order(H, "abc"))
        self.assertEqual(sorted(first), ["G1", "G2"])
        orders = {grading.ab_order(H, f"id{n}") for n in range(40)}
        self.assertEqual(orders, {("G1", "G2"), ("G2", "G1")})


class HistMechanicsTest(unittest.TestCase):
    POOL = [
        {
            "provider": "codex",
            "source_path": "a",
            "source_line": 3,
            "source_ordinal": 1,
            "source_hash": "h3",
        },
        {
            "provider": "codex",
            "source_path": "a",
            "source_line": 3,
            "source_ordinal": 1,
            "source_hash": "h3",
        },
        {
            "provider": "codex",
            "source_path": "b",
            "source_line": 9,
            "source_ordinal": 1,
            "source_hash": "h9",
        },
    ]

    def test_pool_index_matches_reader_cli_dedupe_order(self) -> None:
        refs = grading.pool_refs(self.POOL)
        self.assertEqual(len(refs), 2)
        self.assertEqual(grading.pool_index(refs, self.POOL[2]), 1)
        self.assertIsNone(
            grading.pool_index(refs, dict(self.POOL[2], source_hash="x"))
        )

    def test_hist_opened_needs_an_open_of_that_index_for_that_q(self) -> None:
        trace = [
            {"question": "E1", "command": "events", "args": ["1"]},
            {"question": "E2", "command": "open", "args": ["1"]},
            {"question": "E1", "command": "open", "args": ["1", "2400"]},
        ]
        self.assertTrue(grading.hist_opened(trace, "E1", 1))
        self.assertFalse(grading.hist_opened(trace, "E1", 0))
        self.assertFalse(grading.hist_opened(trace[:2], "E1", 1))


class OpenedIntervalsTest(unittest.TestCase):
    def test_contiguous_coverage_merges_adjacent_pages(self) -> None:
        self.assertTrue(grading.opened_enough([[0, 300], [300, 520]], 900))
        self.assertFalse(grading.opened_enough([[0, 300], [301, 520]], 900))

    def test_short_text_needs_its_whole_length(self) -> None:
        self.assertTrue(grading.opened_enough([[0, 120]], 120))
        self.assertFalse(grading.opened_enough([[10, 120]], 120))
        self.assertFalse(grading.opened_enough([], 120))


class PacketTest(unittest.TestCase):
    QUESTION = {
        "query": "What changed?",
        "answerable": True,
        "required_verbatim": ["the flag moved; the test passed"],
        "atomic_claims": ["the flag moved", "the test passed"],
    }

    def citation(self, line: int, source: str = "s1") -> dict:
        return {
            "role": "assistant",
            "timestamp": f"2026-01-01T00:0{line}:00Z",
            "text": f"text {line}",
            "identity_valid": True,
            "opened": True,
            "in_scope": True,
            "origin": "ORIGINAL",
            "source": source,
            "line": line,
        }

    def packet(
        self, question: dict, citations: list[dict], control: bool = False
    ) -> dict:
        answer = {"answer": "It moved.", "abstained": False}
        return grading.build_packet(
            "abc1234567", question, answer, citations, control=control
        )

    def test_answer_packet_numbers_claims_and_citations(self) -> None:
        packet = self.packet(
            self.QUESTION,
            [self.citation(5), self.citation(2), self.citation(7, "s2")],
        )
        self.assertEqual(packet["answer_id"], "abc1234567")
        self.assertEqual(
            packet["key"]["atomic_claims"],
            [
                {"claim": 1, "text": "the flag moved"},
                {"claim": 2, "text": "the test passed"},
            ],
        )
        self.assertEqual(
            [c["citation"] for c in packet["citations"]], [1, 2, 3]
        )
        orders = [
            (c["same_file_order"], c["same_file_count"])
            for c in packet["citations"]
        ]
        self.assertEqual(orders, [(2, 2), (1, 2), (1, 1)])

    def test_packet_never_carries_arm_source_or_line(self) -> None:
        packet = self.packet(self.QUESTION, [self.citation(5)])
        text = json.dumps(packet)
        for word in ("arm", "HIST", "source", "line", "pool", "unit"):
            self.assertNotIn(f'"{word}"', text)
        self.assertEqual(
            sorted(packet["citations"][0]),
            sorted(
                [
                    "citation",
                    "role",
                    "timestamp",
                    "same_file_order",
                    "same_file_count",
                    "identity_valid",
                    "opened",
                    "in_scope",
                    "origin",
                    "text",
                ]
            ),
        )

    def test_control_packet_is_marked_and_has_no_key(self) -> None:
        packet = self.packet(self.QUESTION, [], control=True)
        self.assertTrue(packet["control"])
        self.assertNotIn("key", packet)


class CitationFixture(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from trial_harness import extractor, origin

        self._tmp = tempfile.TemporaryDirectory()
        mirror = Path(self._tmp.name, "corpus-T0")
        (mirror / ".cl/p/-w").mkdir(parents=True)
        record = {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/w",
            "message": {"role": "assistant", "content": "did it"},
        }
        self.raw = json.dumps(record).encode()
        (mirror / ".cl/p/-w/s.jsonl").write_bytes(self.raw + b"\n")
        roots = [
            {"provider": "claude", "origin": "current", "root": "/h/.cl/p"}
        ]
        self.corpus = extractor.Corpus(
            mirror, {"roots": roots, "entries": []}, "/h"
        )
        rules = {
            "GENERATED_OR_REDACTED": {
                "text_contains_any": [],
                "text_equals": [],
            },
            "INJECTED": {"role": "user", "stripped_text_starts_with_any": []},
        }
        self.index = origin.OriginIndex(self.corpus, rules)
        self.cite = {
            "provider": "claude",
            "source_path": "-w/s.jsonl",
            "source_line": 1,
            "source_ordinal": 1,
            "source_hash": hashlib.sha256(self.raw).hexdigest(),
        }
        self.refs = grading.pool_refs([self.cite])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def record(self, cite: dict, **kw) -> dict:
        args = dict(scope="/w", refs=self.refs, opened=True, fenced=set())
        args.update(kw)
        return grading.old_citation(self.corpus, self.index, cite, **args)


class OldCitationTest(CitationFixture):
    def test_valid_in_pool_citation_is_rendered_by_e(self) -> None:
        item = self.record(self.cite)
        self.assertTrue(item["identity_valid"])
        self.assertTrue(item["role_ok"] and item["in_scope"])
        self.assertEqual(
            (item["text"], item["origin"]), ("did it", "ORIGINAL")
        )
        self.assertEqual(item["identity"]["line"], 1)

    def test_hash_mismatch_is_invalid_and_not_rendered(self) -> None:
        item = self.record(dict(self.cite, source_hash="0" * 64))
        self.assertFalse(item["identity_valid"])
        self.assertEqual((item["text"], item["role"]), ("", None))

    def test_resolved_but_outside_pool_is_invalid(self) -> None:
        item = self.record(self.cite, refs=[])
        self.assertFalse(item["identity_valid"])
        self.assertTrue(item["resolved"])

    def test_out_of_scope_is_flagged(self) -> None:
        self.assertFalse(self.record(self.cite, scope="/other")["in_scope"])

    def test_citation_into_a_fenced_file_fails_fast(self) -> None:
        with self.assertRaises(grading.FencedSourceError):
            self.record(
                dict(self.cite, source_path="-w/gone.jsonl"),
                fenced={("claude", "-w/gone.jsonl")},
            )


def cite(**overrides) -> dict:
    base = {
        "identity_valid": True,
        "opened": True,
        "in_scope": True,
        "role_ok": True,
        "origin": "ORIGINAL",
        "forbidden_match": False,
    }
    base.update(overrides)
    return base


def judged(claims, additions=(), nonconv=(), n=1, substantive=False):
    return {
        "claims": [
            {
                "claim": i,
                "present": present,
                "contradicted": contradicted,
                "support": [{"citation": c, "type": t} for c, t in support],
            }
            for i, (present, contradicted, support) in enumerate(
                claims, start=1
            )
        ],
        "additions": [
            {
                "text": "extra",
                "support": [{"citation": c, "type": t} for c, t in support],
            }
            for support in additions
        ],
        "citations": [
            {"citation": i, "non_conversational": i in nonconv}
            for i in range(1, n + 1)
        ],
        "asserts_substantive_claim": substantive,
    }


FIRST, SECOND = "FIRST_HAND", "SECOND_HAND"


class AnswerRulesTest(unittest.TestCase):
    def rules(self, cites, judgement, abstained=False, format_fail=False):
        return grading.answer_rules(cites, judgement, abstained, format_fail)

    def test_first_hand_original_support_passes_all_rules(self) -> None:
        result = self.rules([cite()], judged([(True, False, [(1, FIRST)])]))
        self.assertEqual(result, {"P": True, "R": True, "X": True})

    def test_second_hand_support_passes_p_and_r_but_not_x(self) -> None:
        result = self.rules([cite()], judged([(True, False, [(1, SECOND)])]))
        self.assertEqual(result, {"P": True, "R": True, "X": False})

    def test_replay_citation_counts_for_p_only(self) -> None:
        result = self.rules(
            [cite(origin="REPLAY_COPY")],
            judged([(True, False, [(1, FIRST)])]),
        )
        self.assertEqual(result, {"P": True, "R": True, "X": False})

    def test_generated_citation_fails_every_rule(self) -> None:
        result = self.rules(
            [cite(origin="GENERATED_OR_REDACTED")],
            judged([(True, False, [(1, FIRST)])]),
        )
        self.assertEqual(result, {"P": False, "R": False, "X": False})

    def test_unsupported_addition_fails_r_and_x_not_p(self) -> None:
        result = self.rules(
            [cite()],
            judged([(True, False, [(1, FIRST)])], additions=[[(1, "NONE")]]),
        )
        self.assertEqual(result, {"P": True, "R": False, "X": False})

    def test_non_conversational_citation_fails_x(self) -> None:
        result = self.rules(
            [cite()], judged([(True, False, [(1, FIRST)])], nonconv={1})
        )
        self.assertEqual(result, {"P": True, "R": True, "X": False})

    def test_every_citation_must_be_valid_even_if_unused(self) -> None:
        result = self.rules(
            [cite(), cite(opened=False)],
            judged([(True, False, [(1, FIRST)])], n=2),
        )
        self.assertEqual(result, {"P": False, "R": False, "X": False})

    def test_missing_or_contradicted_claim_fails(self) -> None:
        for claim in (
            (False, False, [(1, FIRST)]),
            (True, True, [(1, FIRST)]),
        ):
            self.assertEqual(
                self.rules([cite()], judged([claim])),
                {"P": False, "R": False, "X": False},
            )

    def test_abstention_and_format_fail_fail_every_rule(self) -> None:
        good = judged([(True, False, [(1, FIRST)])])
        for kw in ({"abstained": True}, {"format_fail": True}):
            self.assertEqual(
                self.rules([cite()], good, **kw),
                {"P": False, "R": False, "X": False},
            )

    def test_support_from_an_uncited_number_does_not_count(self) -> None:
        result = self.rules([cite()], judged([(True, False, [(2, FIRST)])]))
        self.assertEqual(result, {"P": False, "R": False, "X": False})


class ControlRulesTest(unittest.TestCase):
    def test_w_passes_only_clean_abstention(self) -> None:
        clean = judged([], n=0)
        self.assertTrue(grading.w_rule([], clean, True, False))
        self.assertFalse(grading.w_rule([], clean, False, False))
        self.assertFalse(
            grading.w_rule([cite()], judged([], n=1), True, False)
        )
        self.assertFalse(
            grading.w_rule([], judged([], n=0, substantive=True), True, False)
        )
        self.assertFalse(grading.w_rule([], clean, True, True))

    def test_mirror_fails_on_foreign_or_forbidden_citation(self) -> None:
        for bad in (cite(in_scope=False), cite(forbidden_match=True)):
            judgement = judged([], additions=[[(1, FIRST)]], n=1)
            self.assertFalse(
                grading.mirror_rule([bad], judgement, False, False)
            )

    def test_mirror_passes_supported_answer_or_clean_abstention(self) -> None:
        supported = judged([], additions=[[(1, FIRST)]], n=1)
        self.assertTrue(grading.mirror_rule([cite()], supported, False, False))
        self.assertTrue(grading.mirror_rule([], judged([], n=0), True, False))
        second = judged([], additions=[[(1, SECOND)]], n=1)
        self.assertFalse(grading.mirror_rule([cite()], second, False, False))


class ExtractionTest(unittest.TestCase):
    def test_last_top_level_object_with_exact_keys_wins(self) -> None:
        text = (
            'thinking {"answer": "draft", "citations": [], "abstained": true}'
            ' then {"note": {"answer": "x", "citations": [], '
            '"abstained": false}} final: {"answer": "done", '
            '"citations": [{"a": 1}], "abstained": false} trailing'
        )
        self.assertEqual(
            grading.final_answer(text),
            {"answer": "done", "citations": [{"a": 1}], "abstained": False},
        )

    def test_wrong_types_or_extra_keys_mean_format_fail(self) -> None:
        for text in (
            '{"answer": "a", "citations": [], "abstained": "no"}',
            '{"answer": "a", "citations": [], "abstained": false, "x": 1}',
            "no json at all",
        ):
            self.assertIsNone(grading.final_answer(text))


class GraderOutputTest(unittest.TestCase):
    PACKET = {
        "control": False,
        "key": {"atomic_claims": [{"claim": 1}, {"claim": 2}]},
        "citations": [{"citation": 1}],
    }

    def output(self, **changes) -> str:
        body = judged([(True, False, [(1, FIRST)]), (False, False, [])], n=1)
        body.update(changes)
        return "notes " + json.dumps(body)

    def test_valid_output_parses(self) -> None:
        parsed = grading.parse_grader_output(self.output(), self.PACKET)
        self.assertEqual(len(parsed["claims"]), 2)

    def test_claims_must_cover_the_key_exactly(self) -> None:
        body = json.loads(self.output()[6:])
        body["claims"] = body["claims"][:1]
        with self.assertRaises(grading.GraderOutputError):
            grading.parse_grader_output(json.dumps(body), self.PACKET)

    def test_bad_support_type_or_citation_number_is_rejected(self) -> None:
        for support in (
            [{"citation": 1, "type": "MAYBE"}],
            [{"citation": 9, "type": FIRST}],
        ):
            body = json.loads(self.output()[6:])
            body["claims"][0]["support"] = support
            with self.assertRaises(grading.GraderOutputError):
                grading.parse_grader_output(json.dumps(body), self.PACKET)

    def test_control_output_needs_substantive_flag(self) -> None:
        packet = {"control": True, "citations": []}
        body = judged([], n=0)
        del body["asserts_substantive_claim"]
        with self.assertRaises(grading.GraderOutputError):
            grading.parse_grader_output(json.dumps(body), packet)


class PromptTest(unittest.TestCase):
    RUBRIC = {
        "tolerance_rules": ["T1 rule one", "T2 rule two"],
        "rules": {"definitions": {"support": "SUPPORT DEF TEXT"}},
    }

    def test_grader_prompt_embeds_rules_schema_and_packet(self) -> None:
        prompt = grading.grader_prompt({"answer_id": "x1"}, self.RUBRIC)
        self.assertTrue(prompt.startswith(grading.GRADER_PREAMBLE_HEAD))
        for part in (
            "T1 rule one",
            "T2 rule two",
            "SUPPORT DEF TEXT",
            '"asserts_substantive_claim"',
            '"answer_id": "x1"',
        ):
            self.assertIn(part, prompt)
        self.assertNotIn("{PACKET}", prompt)

    def test_adjudicator_prompt_prefixes_and_orders_a_b(self) -> None:
        prompt = grading.adjudicator_prompt(
            {"answer_id": "x1"}, self.RUBRIC, "OUT-A", "OUT-B"
        )
        self.assertTrue(prompt.startswith(grading.ADJUDICATOR_PREFIX))
        self.assertLess(prompt.index("OUT-A"), prompt.index("OUT-B"))

    def test_preamble_matches_the_protocol_block(self) -> None:
        protocol = (
            "intro\n**Frozen grader preamble.** text\n\n```\n"
            + grading.GRADER_PREAMBLE
            + "\n```\n- **5.4 Adjudication.**"
        )
        self.assertEqual(
            grading.preamble_from_protocol(protocol), grading.GRADER_PREAMBLE
        )


class AdjudicationTest(unittest.TestCase):
    def test_disagreement_on_any_rule_or_control_triggers(self) -> None:
        same = {"P": True, "R": False, "X": False}
        self.assertFalse(grading.needs_adjudication(same, dict(same)))
        self.assertTrue(grading.needs_adjudication(same, dict(same, X=True)))
        self.assertTrue(
            grading.needs_adjudication({"CONTROL": True}, {"CONTROL": False})
        )

    def test_intersection_keeps_only_shared_support(self) -> None:
        a = judged([(True, False, [(1, FIRST)])], additions=[[(1, "NONE")]])
        b = judged([(True, False, [(1, SECOND)])], additions=[])
        both = grading.intersect(a, b)
        self.assertEqual(
            both["claims"][0]["support"], [{"citation": 1, "type": SECOND}]
        )
        self.assertEqual(both["unsupported_additions"], 0)


class KappaTest(unittest.TestCase):
    def test_matches_hand_computed_value(self) -> None:
        a = [True, True, False, False, True, False, True, True]
        b = [True, False, False, False, True, True, True, True]
        # po = 6/8; pe = (5/8)(5/8) + (3/8)(3/8) = 34/64
        result = grading.cohen_kappa(a, b)
        self.assertAlmostEqual(
            result["kappa"], (0.75 - 34 / 64) / (1 - 34 / 64)
        )
        self.assertEqual(result["raw_agreement"], 0.75)
        self.assertEqual(result["n"], 8)

    def test_degenerate_agreement_reports_none(self) -> None:
        result = grading.cohen_kappa([True, True], [True, True])
        self.assertIsNone(result["kappa"])
        self.assertEqual(result["raw_agreement"], 1.0)


class NewCitationTest(CitationFixture):
    def ref(self, **overrides) -> dict:
        ref = {
            "provider": "claude",
            "source_path": "-w/s.jsonl",
            "line": 1,
            "record_sha256": self.cite["source_hash"],
        }
        ref.update(overrides)
        return ref

    def test_opened_forbidden_and_invalid_identities(self) -> None:
        key = ("claude", "-w/s.jsonl", 1, self.cite["source_hash"])
        item = grading.new_citation(
            self.corpus, self.index, self.ref(), "/w", {key}, {key}
        )
        self.assertTrue(item["identity_valid"] and item["opened"])
        self.assertTrue(item["forbidden_match"])
        self.assertEqual(item["text"], "did it")
        bad = grading.new_citation(
            self.corpus,
            self.index,
            self.ref(source_path="-w/x.jsonl"),
            "/w",
            set(),
            set(),
        )
        self.assertFalse(bad["identity_valid"] or bad["role_ok"])
        self.assertEqual(bad["identity_error"], "IDENTITY_INVALID:path")


class CanonicalIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from trial_harness import extractor

        self._tmp = tempfile.TemporaryDirectory()
        self.mirror = Path(self._tmp.name, "corpus-T0")
        self.lines = [b'{"a": 1}', b'{"b": 2}']
        data = b"".join(line + b"\n" for line in self.lines)
        for rel in (
            ".cx/s/2026/r.jsonl",
            ".cx/a/old.jsonl",
            ".cl/p/-w/s.jsonl",
        ):
            (self.mirror / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.mirror / rel).write_bytes(data)
        roots = [
            {"provider": "codex", "origin": "current", "root": "/h/.cx/s"},
            {"provider": "codex", "origin": "archive", "root": "/h/.cx/a"},
            {"provider": "claude", "origin": "current", "root": "/h/.cl/p"},
        ]
        entries = [
            {
                "provider": "codex",
                "resolved_path": "/h/.cx/s/2026/r.jsonl",
                "source_path": "2026/r.jsonl",
            }
        ]
        self.corpus = extractor.Corpus(
            self.mirror, {"roots": roots, "entries": entries}, "/h"
        )
        self.h2 = hashlib.sha256(self.lines[1]).hexdigest()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def canon(self, **ref):
        return grading.canonicalize(self.corpus, ref)

    def test_mirror_original_and_root_relative_paths_agree(self) -> None:
        expected = ("codex", "2026/r.jsonl", 2, self.h2)
        for path in (
            str(self.mirror / ".cx/s/2026/r.jsonl"),
            "/h/.cx/s/2026/r.jsonl",
            "2026/r.jsonl",
        ):
            identity, reason = self.canon(
                provider="codex",
                source_path=path,
                line=2,
                record_sha256=self.h2,
            )
            self.assertIsNone(reason)
            self.assertEqual(tuple(identity.values()), expected)

    def test_archive_and_claude_keys(self) -> None:
        archived, _ = self.canon(
            provider="codex",
            source_path="archive/old.jsonl",
            line=2,
            record_sha256=self.h2,
        )
        self.assertEqual(archived["source_path"], "archive/old.jsonl")
        claude, _ = self.canon(
            provider="claude",
            source_path="-w/s.jsonl",
            line=2,
            record_sha256=self.h2,
        )
        self.assertEqual(claude["source_path"], "-w/s.jsonl")

    def test_byte_offset_must_start_a_line(self) -> None:
        identity, reason = self.canon(
            provider="claude",
            source_path="-w/s.jsonl",
            byte_offset=9,
            record_sha256=self.h2,
        )
        self.assertEqual((identity["line"], reason), (2, None))
        _, reason = self.canon(
            provider="claude",
            source_path="-w/s.jsonl",
            byte_offset=4,
            record_sha256=self.h2,
        )
        self.assertEqual(reason, "IDENTITY_INVALID:offset")

    def test_hash_mismatch_and_unknown_file_are_invalid(self) -> None:
        _, reason = self.canon(
            provider="claude",
            source_path="-w/s.jsonl",
            line=2,
            record_sha256="0" * 64,
        )
        self.assertEqual(reason, "IDENTITY_INVALID:hash")
        _, reason = self.canon(
            provider="claude",
            source_path="-w/none.jsonl",
            line=1,
            record_sha256=self.h2,
        )
        self.assertEqual(reason, "IDENTITY_INVALID:path")


class DecideTest(unittest.TestCase):
    PACKET = {
        "control": False,
        "key": {"atomic_claims": [{"claim": 1, "text": "c"}]},
        "citations": [{"citation": 1}],
    }
    MECH = {
        "kind": "ANSWER",
        "abstained": False,
        "format_fail": False,
        "citations": [cite()],
    }

    def out(self, support: str) -> str:
        return json.dumps(judged([(True, False, [(1, support)])]))

    def test_agreeing_graders_need_no_adjudicator(self) -> None:
        result = grading.decide(
            self.PACKET, self.MECH, self.out(FIRST), self.out(FIRST)
        )
        self.assertFalse(result["adjudicate"])
        self.assertEqual(result["final"], {"P": True, "R": True, "X": True})
        self.assertEqual(result["source"], "AGREED")

    def test_disagreement_waits_for_then_uses_adjudicator(self) -> None:
        pending = grading.decide(
            self.PACKET, self.MECH, self.out(FIRST), self.out(SECOND)
        )
        self.assertEqual(
            (pending["adjudicate"], pending["final"], pending["source"]),
            (True, None, "PENDING_ADJUDICATION"),
        )
        final = grading.decide(
            self.PACKET,
            self.MECH,
            self.out(FIRST),
            self.out(SECOND),
            adjudicator=self.out(SECOND),
        )
        self.assertEqual(final["final"], {"P": True, "R": True, "X": False})
        self.assertEqual(final["source"], "ADJUDICATED")

    def test_unparseable_grader_output_forces_adjudication(self) -> None:
        result = grading.decide(
            self.PACKET, self.MECH, "no json", self.out(FIRST)
        )
        self.assertTrue(result["adjudicate"])
        self.assertIsNotNone(result["g1_error"])

    def test_format_fail_is_mechanical_without_graders(self) -> None:
        result = grading.decide(
            self.PACKET, dict(self.MECH, format_fail=True), None, None
        )
        self.assertEqual(result["final"], {"P": False, "R": False, "X": False})
        self.assertEqual(result["source"], "FORMAT_FAIL")

    def test_w_control_outcome(self) -> None:
        packet = {"control": True, "citations": []}
        mech = {
            "kind": "W",
            "abstained": True,
            "format_fail": False,
            "citations": [],
        }
        clean = json.dumps(judged([], n=0))
        result = grading.decide(packet, mech, clean, clean)
        self.assertEqual(result["final"], {"CONTROL": True})


class ReliabilityTest(unittest.TestCase):
    def test_kappa_per_rule_pooled_and_by_arm_with_controls_apart(
        self,
    ) -> None:
        rows = []
        for arm, pairs in (
            ("OLD", [(1, 1), (0, 0), (1, 0), (0, 0)]),
            ("NEW", [(1, 1), (1, 1), (0, 1), (0, 0)]),
        ):
            for a, b in pairs:
                rows.append(
                    {
                        "arm": arm,
                        "kind": "ANSWER",
                        "g1": {"P": a, "R": a, "X": bool(a)},
                        "g2": {"P": b, "R": b, "X": bool(b)},
                    }
                )
        rows.append(
            {
                "arm": "NEW",
                "kind": "W",
                "g1": {"CONTROL": True},
                "g2": {"CONTROL": False},
            }
        )
        result = grading.reliability(rows)
        pooled = result["answerable"]["X"]["pooled"]
        self.assertEqual((pooled["n"], pooled["raw_agreement"]), (8, 0.75))
        self.assertEqual(result["answerable"]["X"]["by_arm"]["OLD"]["n"], 4)
        self.assertEqual(result["controls"]["pooled"]["n"], 1)
        self.assertTrue(result["low_grader_agreement"])


class GradingCliTest(unittest.TestCase):
    def test_decide_reads_files_and_prints_final_rules(self) -> None:
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "packet.json": DecideTest.PACKET,
                "mech.json": DecideTest.MECH,
            }
            for name, value in files.items():
                (root / name).write_text(json.dumps(value))
            answer = json.dumps(judged([(True, False, [(1, FIRST)])]))
            for name in ("g1.txt", "g2.txt"):
                (root / name).write_text(answer)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = grading.main(
                    ["decide"]
                    + [
                        str(root / n)
                        for n in (
                            "packet.json",
                            "mech.json",
                            "g1.txt",
                            "g2.txt",
                        )
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(out.getvalue())["final"],
                {"P": True, "R": True, "X": True},
            )


if __name__ == "__main__":
    unittest.main()
