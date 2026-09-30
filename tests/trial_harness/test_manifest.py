"""Stage-B freeze manifest writer, verifier and post-hoc re-hash."""

from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from trial_harness import manifest


class HashingTest(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "a.bin")
            path.write_bytes(b"x" * 3_000_000)
            self.assertEqual(
                manifest.sha256_file(path),
                hashlib.sha256(b"x" * 3_000_000).hexdigest(),
            )

    def test_tree_hashes_skips_pycache_and_keys_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg/__pycache__").mkdir(parents=True)
            (root / "pkg/__pycache__/m.pyc").write_bytes(b"cache")
            (root / "pkg/m.py").write_text("print(1)\n")
            (root / "top.txt").write_text("t")
            tree = manifest.tree_hashes(root)
        self.assertEqual(sorted(tree), ["pkg/m.py", "top.txt"])
        self.assertEqual(tree["top.txt"], hashlib.sha256(b"t").hexdigest())

    def test_write_private_uses_owner_only_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "new/dir/out.json")
            manifest.write_json(path, {"b": 1, "a": [1]})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for directory in (path.parent, path.parent.parent):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(json.loads(path.read_text()), {"a": [1], "b": 1})


class StageBTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.trial = self.root / "trial"
        self.repo = self.root / "repo"
        (self.repo / "v7").mkdir(parents=True)
        (self.repo / "v7/labels.json").write_text("{}")
        (self.repo / "notes.md").write_text("n")
        self.code = self.root / "code.py"
        self.code.write_text("x = 1\n")
        self.built = manifest.build_stage_b(
            files={"harness_code": [self.code]},
            trees={"repo_tree": self.repo},
            values={"unit_order": ["OLD:E1", "NEW:E1"]},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_records_manifest_sha_in_frozen_b(self) -> None:
        digest = manifest.write_stage_b(self.trial, self.built)
        frozen = (self.trial / "FROZEN-B.sha256").read_text()
        self.assertEqual(frozen, f"{digest}  freeze-manifest.json\n")
        self.assertEqual(
            digest,
            manifest.sha256_file(self.trial / "freeze-manifest.json"),
        )

    def test_write_refuses_to_replace_an_existing_freeze(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        with self.assertRaises(FileExistsError):
            manifest.write_stage_b(self.trial, self.built)

    def test_verify_is_clean_when_nothing_changed(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        self.assertEqual(manifest.verify_stage_b(self.trial), [])

    def test_verify_reports_a_changed_file_as_deviation(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        self.code.write_text("x = 2\n")
        deviations = manifest.verify_stage_b(self.trial)
        self.assertEqual(len(deviations), 1)
        self.assertEqual(deviations[0]["section"], "harness_code")
        self.assertEqual(deviations[0]["kind"], "CHANGED")

    def test_verify_reports_added_and_removed_tree_files(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        (self.repo / "notes.md").unlink()
        (self.repo / "extra.txt").write_text("e")
        kinds = {
            (d["path"], d["kind"]) for d in manifest.verify_stage_b(self.trial)
        }
        self.assertEqual(
            kinds, {("notes.md", "REMOVED"), ("extra.txt", "ADDED")}
        )

    def test_verify_rejects_an_edited_manifest(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        path = self.trial / "freeze-manifest.json"
        path.chmod(0o600)
        path.write_text(path.read_text().replace("OLD:E1", "OLD:E2"))
        with self.assertRaises(ValueError):
            manifest.verify_stage_b(self.trial)

    def test_posthoc_voids_when_a_frozen_repo_input_changed(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        (self.repo / "v7/labels.json").write_text('{"x": 1}')
        verdict = manifest.posthoc_rehash(
            self.trial, frozen_inputs={"v7/labels.json"}
        )
        self.assertEqual(verdict["status"], "VOID")

    def test_posthoc_marks_other_changes_as_deviation(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        (self.repo / "notes.md").write_text("changed")
        verdict = manifest.posthoc_rehash(
            self.trial, frozen_inputs={"v7/labels.json"}
        )
        self.assertEqual(verdict["status"], "DEVIATION")
        self.assertEqual(len(verdict["deviations"]), 1)

    def test_posthoc_is_clean_without_changes(self) -> None:
        manifest.write_stage_b(self.trial, self.built)
        verdict = manifest.posthoc_rehash(
            self.trial, frozen_inputs={"v7/labels.json"}
        )
        self.assertEqual(verdict, {"status": "CLEAN", "deviations": []})


class ManifestCliTest(unittest.TestCase):
    def test_write_then_verify_then_posthoc(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            (root / "repo/a.txt").write_text("a")
            spec = root / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "files": {"prompts": [str(root / "repo/a.txt")]},
                        "trees": {"repo_tree": str(root / "repo")},
                        "values": {"unit_order": ["OLD:E1"]},
                    }
                )
            )
            trial = str(root / "trial")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    manifest.main(["write", str(spec), "--trial", trial]), 0
                )
                self.assertEqual(
                    manifest.main(["verify", "--trial", trial]), 0
                )
                (root / "repo/a.txt").write_text("changed")
                self.assertEqual(
                    manifest.main(["verify", "--trial", trial]), 1
                )
                self.assertEqual(
                    manifest.main(["posthoc", "--trial", trial]), 1
                )
            self.assertIn("DEVIATION", out.getvalue())


if __name__ == "__main__":
    unittest.main()
