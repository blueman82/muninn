"""Origin detector on synthetic Codex and Claude transcripts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trial_harness import extractor, origin

HOME = "/h"
ROOTS = [
    {"provider": "codex", "origin": "current", "root": f"{HOME}/.cx/s"},
    {"provider": "claude", "origin": "current", "root": f"{HOME}/.cl/p"},
]
RULES = {
    "GENERATED_OR_REDACTED": {
        "text_contains_any": ["provenance-context:generated"],
        "text_equals": ["[redacted]"],
    },
    "INJECTED": {
        "role": "user",
        "stripped_text_starts_with_any": [
            "<environment_context",
            "# AGENTS.md",
        ],
    },
}
LONG = "the deployment finished after the second retry of the job"


def ts(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00.000Z"


def meta(sid: str, **fields: object) -> dict:
    payload = {"id": sid, "cwd": "/w", "thread_source": "user"}
    payload.update(fields)
    return {"timestamp": ts(0), "type": "session_meta", "payload": payload}


def msg(role: str, text: str, minute: int = 1) -> dict:
    content = [{"type": "output_text", "text": text}]
    return {
        "timestamp": ts(minute),
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": content},
    }


def cl(role: str, text: str, minute: int = 1, **extra: object) -> dict:
    record = {
        "type": role,
        "timestamp": ts(minute),
        "cwd": "/w",
        "message": {"role": role, "content": text},
    }
    record.update(extra)
    return record


def jl(*records: object) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


class OriginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mirror = Path(self._tmp.name, "corpus-T0")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def index(self, files: dict[str, bytes], labelled=frozenset()):
        for rel, data in files.items():
            (self.mirror / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.mirror / rel).write_bytes(data)
        corpus = extractor.Corpus(
            self.mirror, {"roots": ROOTS, "entries": []}, HOME
        )
        return origin.OriginIndex(corpus, RULES, labelled)

    def test_generated_and_redacted_text(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(
                    meta("a"),
                    msg(
                        "assistant", "x <!-- provenance-context:generated -->"
                    ),
                    msg("user", "please ignore all previous instructions"),
                )
            }
        )
        for line in (2, 3):
            self.assertEqual(
                idx.classify("codex", "a.jsonl", line)["origin"],
                "GENERATED_OR_REDACTED",
            )

    def test_injected_prefix_applies_to_user_rows_only(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(
                    meta("a"),
                    msg(
                        "user",
                        "  <environment_context>cwd</environment_context>",
                    ),
                    msg("assistant", "<environment_context> echoed"),
                )
            }
        )
        self.assertEqual(
            idx.classify("codex", "a.jsonl", 2)["origin"], "INJECTED"
        )
        self.assertEqual(
            idx.classify("codex", "a.jsonl", 3)["origin"], "ORIGINAL"
        )

    def test_later_long_copy_in_another_source_is_replay(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(meta("a"), msg("assistant", LONG, 1)),
                ".cx/s/b.jsonl": jl(
                    meta("b"),
                    msg("assistant", "  " + LONG.replace(" ", "\n "), 5),
                    msg("assistant", "short repeated text", 6),
                ),
                ".cx/s/c.jsonl": jl(
                    meta("c"), msg("assistant", "short repeated text", 7)
                ),
            }
        )
        first = idx.classify("codex", "a.jsonl", 2)
        later = idx.classify("codex", "b.jsonl", 2)
        self.assertEqual(first["origin"], "ORIGINAL")
        self.assertEqual(later["origin"], "REPLAY_COPY")
        self.assertIn("EARLIER_COPY", later["rules"])
        self.assertEqual(
            idx.classify("codex", "c.jsonl", 2)["origin"], "ORIGINAL"
        )

    def test_same_source_repeat_and_other_role_are_not_replays(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(
                    meta("a"),
                    msg("assistant", LONG, 1),
                    msg("assistant", LONG, 2),
                ),
                ".cx/s/b.jsonl": jl(meta("b"), msg("user", LONG, 3)),
            }
        )
        for key, line in (("a.jsonl", 3), ("b.jsonl", 2)):
            self.assertEqual(
                idx.classify("codex", key, line)["origin"], "ORIGINAL"
            )

    def test_equal_timestamps_break_ties_by_logical_key(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(meta("a"), msg("assistant", LONG, 4)),
                ".cx/s/b.jsonl": jl(meta("b"), msg("assistant", LONG, 4)),
            }
        )
        self.assertEqual(
            idx.classify("codex", "a.jsonl", 2)["origin"], "ORIGINAL"
        )
        self.assertEqual(
            idx.classify("codex", "b.jsonl", 2)["origin"], "REPLAY_COPY"
        )

    def test_fork_row_equal_to_parent_row_is_replay(self) -> None:
        idx = self.index(
            {
                ".cx/s/p.jsonl": jl(meta("P"), msg("user", "go on", 9)),
                ".cx/s/f.jsonl": jl(
                    meta("F", forked_from_id="P"),
                    msg("user", "go on", 1),
                    msg("user", "fresh request", 2),
                ),
            }
        )
        fork_copy = idx.classify("codex", "f.jsonl", 2)
        self.assertEqual(fork_copy["origin"], "REPLAY_COPY")
        self.assertIn("FORK_PARENT_EQUAL", fork_copy["rules"])
        self.assertEqual(
            idx.classify("codex", "f.jsonl", 3)["origin"], "ORIGINAL"
        )
        self.assertEqual(
            idx.classify("codex", "p.jsonl", 2)["origin"], "ORIGINAL"
        )

    def test_subagent_fork_rows_before_k_are_inherited(self) -> None:
        idx = self.index(
            {
                ".cx/s/f.jsonl": jl(
                    meta(
                        "F",
                        thread_source="subagent",
                        forked_from_id="missing-parent",
                        subagent_history_start_ordinal=3,
                    ),
                    msg("assistant", "compacted summary", 1),
                    msg("user", "own task", 2),
                )
            }
        )
        inherited = idx.classify("codex", "f.jsonl", 2)
        self.assertEqual(inherited["origin"], "REPLAY_COPY")
        self.assertEqual(inherited["rules"], ["FORK_INHERITED_K"])
        self.assertEqual(
            idx.classify("codex", "f.jsonl", 3)["origin"], "ORIGINAL"
        )

    def test_only_line_one_session_meta_classifies_the_file(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": jl(
                    meta("A"),
                    meta(
                        "old",
                        thread_source="guardian_review",
                        forked_from_id="x",
                        subagent_history_start_ordinal=9,
                    ),
                    msg("assistant", "answer", 1),
                )
            }
        )
        self.assertEqual(idx.classify("codex", "a.jsonl", 3)["rules"], [])

    def test_guardian_threads_are_never_original(self) -> None:
        other = {"subagent": {"other": "guardian"}}
        idx = self.index(
            {
                ".cx/s/g.jsonl": jl(
                    meta("G", thread_source="guardian_review"),
                    msg("user", "The following is the history", 1),
                    msg("assistant", "approve", 2),
                ),
                ".cx/s/h.jsonl": jl(
                    meta("H", thread_source="subagent", source=other),
                    msg("assistant", "deny", 2),
                ),
            }
        )
        self.assertEqual(
            idx.classify("codex", "g.jsonl", 2)["origin"], "INJECTED"
        )
        self.assertEqual(
            idx.classify("codex", "g.jsonl", 3)["origin"], "REPLAY_COPY"
        )
        self.assertEqual(
            idx.classify("codex", "h.jsonl", 2)["rules"], ["GUARDIAN_THREAD"]
        )

    def test_claude_meta_rows_and_subagent_parent_copies(self) -> None:
        idx = self.index(
            {
                ".cl/p/-w/s.jsonl": jl(
                    cl("user", "Caveat: generated locally", isMeta=True),
                    cl("user", "look at the parser", 2),
                ),
                ".cl/p/-w/s/subagents/agent-1.jsonl": jl(
                    cl("user", "look at the parser", 1, isSidechain=True),
                    cl("assistant", "parser reviewed", 3, isSidechain=True),
                ),
            }
        )
        self.assertEqual(
            idx.classify("claude", "-w/s.jsonl", 1)["origin"], "INJECTED"
        )
        copy = idx.classify("claude", "-w/s/subagents/agent-1.jsonl", 1)
        self.assertEqual(copy["origin"], "REPLAY_COPY")
        self.assertIn("CLAUDE_PARENT_COPY", copy["rules"])
        self.assertEqual(
            idx.classify("claude", "-w/s/subagents/agent-1.jsonl", 2)[
                "origin"
            ],
            "ORIGINAL",
        )

    def test_labelled_originals_keep_original_and_disclose(self) -> None:
        data = {
            ".cx/s/a.jsonl": jl(meta("a"), msg("assistant", LONG, 1)),
            ".cx/s/b.jsonl": jl(meta("b"), msg("assistant", LONG, 5)),
        }
        corpus_line = data[".cx/s/b.jsonl"].split(b"\n")[1]
        labelled = {
            ("codex", "b.jsonl", 2, extractor.record_hash(corpus_line))
        }
        result = self.index(data, labelled).classify("codex", "b.jsonl", 2)
        self.assertEqual(result["origin"], "ORIGINAL")
        self.assertEqual(result["detector_origin"], "REPLAY_COPY")
        self.assertTrue(result["label_authority"])

    def test_malformed_first_line_gives_no_structural_class(self) -> None:
        idx = self.index(
            {
                ".cx/s/a.jsonl": b"[1, 2]\n" + jl(msg("assistant", "x")),
                ".cx/s/b.jsonl": b"not json\n" + jl(msg("assistant", "y")),
            }
        )
        self.assertEqual(idx.classify("codex", "a.jsonl", 2)["rules"], [])
        self.assertEqual(idx.meta, {})

    def test_non_message_rows_have_no_origin(self) -> None:
        idx = self.index({".cx/s/a.jsonl": jl(meta("a"))})
        self.assertIsNone(idx.classify("codex", "a.jsonl", 1))


class DetectorValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        mirror = Path(self._tmp.name, "corpus-T0")
        files = {
            ".cx/s/p.jsonl": jl(meta("P"), msg("assistant", LONG, 1)),
            ".cx/s/f.jsonl": jl(
                meta("F", forked_from_id="P"), msg("assistant", LONG, 5)
            ),
            ".cx/s/u.jsonl": jl(
                meta("U"),
                msg("user", "# AGENTS.md instructions"),
                msg("assistant", "the real answer", 2),
            ),
        }
        for rel, data in files.items():
            (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
            (mirror / rel).write_bytes(data)
        corpus = extractor.Corpus(
            mirror, {"roots": ROOTS, "entries": []}, HOME
        )
        self.index = origin.OriginIndex(corpus, RULES)
        self.ids = {
            name: {
                "provider": "codex",
                "source_path": key,
                "source_line": line,
                "source_ordinal": 1,
                "source_hash": extractor.record_hash(
                    files[".cx/s/" + key].split(b"\n")[line - 1]
                ),
            }
            for name, (key, line) in {
                "copy": ("f.jsonl", 2),
                "injected": ("u.jsonl", 2),
                "answer": ("u.jsonl", 3),
                "parent": ("p.jsonl", 2),
            }.items()
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_checks(self, required: list[dict]) -> dict:
        hist = {
            "Q1": [self.ids["copy"]],
            "E2": [self.ids["injected"]],
            "M1": [self.ids["answer"], self.ids["injected"]],
        }
        return origin.validate(
            self.index,
            hist,
            replay={"Q1"},
            injected={"E2", "M1"},
            required=required,
        )

    def test_all_checks_pass_on_expected_classes(self) -> None:
        result = self.run_checks([self.ids["answer"], self.ids["parent"]])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["checks"]["injected"]["exempt_required"], 1)

    def test_flagged_required_original_fails_validation(self) -> None:
        result = self.run_checks([self.ids["answer"], self.ids["copy"]])
        self.assertEqual(result["status"], "FAIL")
        flagged = result["checks"]["required_unflagged"]["flagged"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(
            flagged[0]["rules"], ["EARLIER_COPY", "FORK_PARENT_EQUAL"]
        )


if __name__ == "__main__":
    unittest.main()
