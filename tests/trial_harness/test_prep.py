"""Trial data preparation: OLD-arm copies, T0 mirror, assignment."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from trial_harness import prep


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OldArmCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo, self.dest = root / "repo", root / "trial/old-arm"
        self.files = {
            "v7-evidence/round4/tool.py": b"print('tool')\n",
            "v7-evidence/labels.json": b"{}",
        }
        for rel, data in self.files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_bytes(data)
        self.expected = {rel: sha(data) for rel, data in self.files.items()}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_copies_are_real_read_only_files_with_equal_hashes(self) -> None:
        prep.copy_old_arm(self.repo, self.dest, self.expected)
        for rel, data in self.files.items():
            copy, original = self.dest / rel, self.repo / rel
            self.assertFalse(copy.is_symlink())
            self.assertEqual(copy.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o444)
            self.assertNotEqual(copy.stat().st_ino, original.stat().st_ino)
            self.assertEqual(copy.stat().st_nlink, 1)
        for directory in (self.dest, self.dest / "v7-evidence"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_hashes_ok_lists_every_copy_in_shasum_format(self) -> None:
        prep.copy_old_arm(self.repo, self.dest, self.expected)
        lines = (self.dest / "HASHES.ok").read_text().splitlines()
        self.assertEqual(
            sorted(lines),
            sorted(f"{h}  {rel}" for rel, h in self.expected.items()),
        )

    def test_hash_mismatch_raises_and_writes_no_ok_file(self) -> None:
        bad = dict(self.expected, **{"v7-evidence/labels.json": "0" * 64})
        with self.assertRaises(ValueError):
            prep.copy_old_arm(self.repo, self.dest, bad)
        self.assertFalse((self.dest / "HASHES.ok").exists())

    def test_rerun_verifies_existing_copies(self) -> None:
        prep.copy_old_arm(self.repo, self.dest, self.expected)
        (self.dest / "HASHES.ok").chmod(0o600)
        (self.dest / "HASHES.ok").unlink()
        prep.copy_old_arm(self.repo, self.dest, self.expected)
        self.assertTrue((self.dest / "HASHES.ok").exists())


class MirrorVerifyTest(unittest.TestCase):
    def test_reports_missing_changed_and_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = root / "corpus-T0"
            (mirror / "a").mkdir(parents=True)
            (mirror / "a/ok.jsonl").write_bytes(b"ok\n")
            (mirror / "a/changed.jsonl").write_bytes(b"new\n")
            (mirror / "a/extra.jsonl").write_bytes(b"x\n")
            tsv = root / "m.tsv"
            tsv.write_text(
                "# T0=... files=3\n"
                f"a/ok.jsonl\t3\t{sha(b'ok' + bytes([10]))}\n"
                f"a/changed.jsonl\t4\t{sha(b'old' + bytes([10]))}\n"
                f"a/gone.jsonl\t2\t{sha(b'g' + bytes([10]))}\n"
            )
            result = prep.verify_mirror(mirror, tsv)
        self.assertEqual(result["listed"], 3)
        self.assertEqual(result["missing"], ["a/gone.jsonl"])
        self.assertEqual(result["changed"], ["a/changed.jsonl"])
        self.assertEqual(result["unlisted"], ["a/extra.jsonl"])
        self.assertFalse(result["ok"])

    def test_fenced_deletions_are_expected_absences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirror = root / "corpus-T0"
            mirror.mkdir()
            (mirror / "kept.jsonl").write_bytes(b"k\n")
            tsv = root / "m.tsv"
            tsv.write_text(
                f"kept.jsonl\t2\t{sha(b'k' + bytes([10]))}\n"
                f"fenced.jsonl\t2\t{sha(b'f' + bytes([10]))}\n"
            )
            result = prep.verify_mirror(mirror, tsv, fenced={"fenced.jsonl"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])


class AssignmentTest(unittest.TestCase):
    QIDS = ["E1", "E2", "W1"]
    ANSWERABLE = {"E1", "E2"}

    def test_units_cover_old_new_and_mirror_controls(self) -> None:
        units = prep.unit_ids(self.QIDS, self.ANSWERABLE)
        self.assertEqual(
            sorted(units),
            sorted(
                [
                    "OLD:E1",
                    "OLD:E2",
                    "OLD:W1",
                    "NEW:E1",
                    "NEW:E2",
                    "NEW:W1",
                    "NEW:E1:MIRROR",
                    "NEW:E2:MIRROR",
                ]
            ),
        )

    def test_order_is_sorted_then_shuffled_with_protocol_seed(self) -> None:
        import random

        units = prep.unit_ids(self.QIDS, self.ANSWERABLE)
        digest = "ab" * 32
        expected = sorted(units)
        random.Random(int(digest[0:16], 16)).shuffle(expected)
        result = prep.assignment(units, digest)
        self.assertEqual(result["order"], expected)
        self.assertEqual(result["seed"], int(digest[0:16], 16))
        self.assertEqual(result["seed_hex"], digest[0:16])


class ProtocolDigestTest(unittest.TestCase):
    def test_reads_digest_of_protocol_from_frozen_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            (trial / "trial-protocol.md").write_bytes(b"protocol")
            (trial / "FROZEN.sha256").write_text(
                f"{sha(b'protocol')}  trial-protocol.md\n"
                f"{'1' * 64}  grading-rubric.json\n"
            )
            self.assertEqual(prep.protocol_digest(trial), sha(b"protocol"))
            os.chmod(trial / "trial-protocol.md", 0o600)
            (trial / "trial-protocol.md").write_bytes(b"edited")
            with self.assertRaises(ValueError):
                prep.protocol_digest(trial)


def ident(path: str, line: int, digest: str) -> dict:
    return {
        "provider": "claude",
        "source_path": path,
        "source_line": line,
        "source_ordinal": 1,
        "source_hash": digest,
    }


class AvailabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        mirror = Path(self._tmp.name, "corpus-T0")
        (mirror / ".cl/p/-x").mkdir(parents=True)
        self.lines = [b'{"n": 1}', b'{"n": 2}']
        data = b"".join(line + bytes([10]) for line in self.lines)
        (mirror / ".cl/p/-x/a.jsonl").write_bytes(data + b'{"late": 1}\n')
        entries = [
            {
                "provider": "claude",
                "resolved_path": "/h/.cl/p/-x/a.jsonl",
                "source_path": "-x/a.jsonl",
                "whole_file_bytes": len(data),
                "whole_file_sha256": sha(data),
            }
        ]
        inventory = {
            "roots": [
                {"provider": "claude", "origin": "current", "root": "/h/.cl/p"}
            ],
            "entries": entries,
        }
        from trial_harness.extractor import Corpus

        self.corpus = Corpus(mirror, inventory, home="/h")
        self.entries = entries

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def questions(self) -> dict:
        return {
            "E1": {
                "required_originals": [
                    ident("-x/a.jsonl", 2, sha(self.lines[1]))
                ],
                "mirror_control": {
                    "forbidden_originals": [
                        ident("-x/a.jsonl", 2, sha(self.lines[1]))
                    ]
                },
                "control_expectation": {"rule": "ANSWER"},
            },
            "E2": {
                "required_originals": [ident("-x/a.jsonl", 1, "0" * 64)],
                "control_expectation": {"rule": "ANSWER"},
            },
            "W1": {
                "control_expectation": {
                    "rule": "W",
                    "forbidden_originals": [
                        ident("-x/missing.jsonl", 1, sha(b"x"))
                    ],
                }
            },
        }

    def test_labelled_identities_are_distinct_with_kind(self) -> None:
        ids = prep.labelled_identities(self.questions())
        kinds = sorted((i["questions"], i["kind"]) for i in ids.values())
        self.assertEqual(
            kinds,
            [
                (["E1"], "required"),
                (["E2"], "required"),
                (["W1"], "forbidden"),
            ],
        )

    def test_drift_excludes_questions_with_any_unavailable_identity(
        self,
    ) -> None:
        result = prep.availability(self.corpus, self.entries, self.questions())
        self.assertEqual(result["corpus_drift"], ["E2", "W1"])
        by_q = {i["questions"][0]: i for i in result["identities"]}
        self.assertTrue(by_q["E1"]["available"])
        self.assertTrue(by_q["E1"]["prefix_hash_ok"])
        self.assertFalse(by_q["E2"]["line_hash_ok"])
        self.assertFalse(by_q["W1"]["file_present"])
        self.assertEqual(result["summary"]["available"], 1)

    def test_changed_captured_prefix_is_drift(self) -> None:
        entries = [dict(self.entries[0], whole_file_sha256="1" * 64)]
        result = prep.availability(self.corpus, entries, self.questions())
        self.assertIn("E1", result["corpus_drift"])


if __name__ == "__main__":
    unittest.main()
