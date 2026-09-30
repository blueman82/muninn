"""Mechanical T0 fence F1/F2/G1 on synthetic transcripts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trial_harness import extractor, fence

HOME = "/home/u"
ROOTS = [
    {"provider": "codex", "origin": "current", "root": f"{HOME}/.cx/s"},
    {"provider": "claude", "origin": "current", "root": f"{HOME}/.cl/p"},
]
DEV = "/Users/garyharr/Github/provenance-context"
NEEDLES = {"query:E1": "which flag enabled the fast path"}


def jl(*records: object) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


class FenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mirror = Path(self._tmp.name, "corpus-T0")
        self.listed: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, rel: str, data: bytes, listed: bool = False) -> None:
        path = self.mirror / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if listed:
            provider = "codex" if rel.startswith(".cx") else "claude"
            root = next(r["root"] for r in ROOTS if r["provider"] == provider)
            self.listed.append(
                {
                    "provider": provider,
                    "resolved_path": f"{HOME}/{rel}",
                    "source_path": f"{HOME}/{rel}"[len(root) + 1 :],
                }
            )

    def rows(self, labelled: dict | None = None) -> dict[str, dict]:
        corpus = extractor.Corpus(
            self.mirror, {"roots": ROOTS, "entries": self.listed}, HOME
        )
        rows = fence.compute(corpus, NEEDLES, labelled or {})
        return {row["path"]: row for row in rows}

    def test_f1_claude_record_cwd_under_dev_repo_string_prefix(self) -> None:
        self.add(
            ".cl/p/-x/a.jsonl",
            jl({"type": "user", "cwd": f"{DEV}-build"}),
            listed=True,
        )
        row = self.rows()[".cl/p/-x/a.jsonl"]
        self.assertEqual(row["reasons"], ["F1"])
        self.assertEqual(row["action"], "DELETE")

    def test_f1_codex_payload_cwd_under_coderails(self) -> None:
        self.add(
            ".cx/s/2026/r.jsonl",
            jl(
                {"type": "session_meta", "payload": {"cwd": "/tmp/w"}},
                {
                    "type": "turn_context",
                    "payload": {"cwd": "/Users/garyharr/.coderails/wt/1"},
                },
            ),
            listed=True,
        )
        self.assertEqual(self.rows()[".cx/s/2026/r.jsonl"]["reasons"], ["F1"])

    def test_nested_tool_cwd_is_not_a_record_cwd(self) -> None:
        self.add(
            ".cx/s/2026/r.jsonl",
            jl(
                {"type": "session_meta", "payload": {"cwd": "/tmp/w"}},
                {"type": "event_msg", "payload": {"item": {"cwd": DEV}}},
            ),
            listed=True,
        )
        self.assertEqual(self.rows(), {})

    def test_f2_whitespace_normalised_match_in_unlisted_file(self) -> None:
        text = "log: Which flag?\n which  flag\n\tenabled the fast path now"
        self.add(
            ".cx/s/2026/new.jsonl",
            jl({"type": "response_item", "payload": {"content": [text]}}),
        )
        row = self.rows()[".cx/s/2026/new.jsonl"]
        self.assertEqual(row["reasons"], ["F2"])
        self.assertEqual(row["detail"], "F2:query:E1")

    def test_f2_ignores_inventory_listed_files(self) -> None:
        self.add(
            ".cx/s/2026/old.jsonl",
            jl({"text": "which flag enabled the fast path"}),
            listed=True,
        )
        self.assertEqual(self.rows(), {})

    def test_f2_reads_undecodable_lines_as_raw_text(self) -> None:
        self.add(
            ".cl/p/-x/b.jsonl",
            b"not json: which flag enabled the fast path\n",
        )
        self.assertEqual(self.rows()[".cl/p/-x/b.jsonl"]["reasons"], ["F2"])

    def test_g1_keeps_a_labelled_file_and_flags_its_question(self) -> None:
        self.add(
            ".cl/p/-x/a.jsonl",
            jl({"type": "user", "cwd": DEV}),
            listed=True,
        )
        row = self.rows({("claude", "-x/a.jsonl"): {"E1"}})[".cl/p/-x/a.jsonl"]
        self.assertEqual(row["action"], "KEEP_G1")
        self.assertEqual(row["flagged_questions"], ["E1"])

    def test_row_records_sha256_of_the_clone(self) -> None:
        data = jl({"type": "user", "cwd": DEV})
        self.add(".cl/p/-x/a.jsonl", data, listed=True)
        self.assertEqual(
            self.rows()[".cl/p/-x/a.jsonl"]["sha256"],
            hashlib.sha256(data).hexdigest(),
        )


class ApplyFenceTest(unittest.TestCase):
    def test_deletes_only_delete_rows_after_hash_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            for name in ("gone.jsonl", "kept.jsonl"):
                (mirror / name).write_bytes(name.encode())
                (mirror / name).chmod(0o444)
            rows = [
                {
                    "path": "gone.jsonl",
                    "action": "DELETE",
                    "sha256": hashlib.sha256(b"gone.jsonl").hexdigest(),
                },
                {"path": "kept.jsonl", "action": "KEEP_G1", "sha256": "x"},
            ]
            self.assertEqual(fence.apply(mirror, rows), 1)
            self.assertFalse((mirror / "gone.jsonl").exists())
            self.assertTrue((mirror / "kept.jsonl").exists())

    def test_hash_mismatch_aborts_before_any_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror = Path(tmp)
            (mirror / "a.jsonl").write_bytes(b"a")
            rows = [{"path": "a.jsonl", "action": "DELETE", "sha256": "0"}]
            with self.assertRaises(ValueError):
                fence.apply(mirror, rows)
            self.assertTrue((mirror / "a.jsonl").exists())


class FenceListTest(unittest.TestCase):
    def test_tsv_lists_path_reason_detail_sha_action(self) -> None:
        rows = [
            {
                "path": "a.jsonl",
                "reasons": ["F1", "F2"],
                "detail": "F1:/x;F2:query:E1",
                "sha256": "ab",
                "bytes": 3,
                "action": "DELETE",
                "flagged_questions": [],
            }
        ]
        text = fence.tsv(rows)
        self.assertEqual(
            text.splitlines(),
            [
                "path\treasons\tdetail\tsha256\tbytes\taction\tflagged",
                "a.jsonl\tF1+F2\tF1:/x;F2:query:E1\tab\t3\tDELETE\t",
            ],
        )


if __name__ == "__main__":
    unittest.main()
