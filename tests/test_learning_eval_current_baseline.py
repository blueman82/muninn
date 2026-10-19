"""Check fixed baseline boundaries without depending on a legacy checkout."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.learning_eval import current_baseline as baseline
from tools.learning_eval import current_baseline_child as child
from tools.learning_eval import current_baseline_models as models
from tools.learning_eval.cases import digest
from tools.learning_eval.current_baseline_models import BaselineManifest


class BaselineBoundaryTests(unittest.TestCase):
    """Exercise source confinement, strict messages and immutable replay."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "muninn").mkdir(parents=True)
        self.module = self.snapshot / "muninn" / "__init__.py"
        self.module.write_bytes(b'"""Harmless fixture."""\n')
        self.files = {
            "muninn/__init__.py": hashlib.sha256(
                self.module.read_bytes()
            ).hexdigest()
        }

    def test_snapshot_rejects_changed_missing_extra_and_symlink(self) -> None:
        child.verify_snapshot(self.snapshot, self.root, self.files)
        self.module.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source_binding"):
            child.verify_snapshot(self.snapshot, self.root, self.files)
        self.module.unlink()
        with self.assertRaises(ValueError):
            child.verify_snapshot(self.snapshot, self.root, self.files)
        self.module.write_bytes(b'"""Harmless fixture."""\n')
        extra = self.snapshot / "muninn" / "extra.py"
        extra.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "source_binding"):
            child.verify_snapshot(self.snapshot, self.root, self.files)
        extra.unlink()
        self.module.unlink()
        self.module.symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            child.verify_snapshot(self.snapshot, self.root, self.files)

    def test_confined_paths_and_manifest_reject_escape(self) -> None:
        with self.assertRaises(ValueError):
            child.confined(self.root.parent, self.root)
        with self.assertRaises(ValueError):
            child.confined(self.root / ".." / "escape", self.root)
        with self.assertRaises(ValueError):
            BaselineManifest.model_validate_json('{"operation":"save"}')

    def test_json_refuses_duplicates_nonfinite_and_oversize(self) -> None:
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                child.decode(raw, 100)
        with self.assertRaises(ValueError):
            child.decode(b" " * 101, 100)

    def test_unknown_operation_refuses_before_import(self) -> None:
        with (
            mock.patch.object(child.importlib, "import_module") as imported,
            self.assertRaises(ValueError),
        ):
            child.execute({"operation": "know-add"})
        imported.assert_not_called()

    def test_output_bound_preserves_prefix(self) -> None:
        output = child.BoundedOutput(4)
        output.write("abc")
        with self.assertRaisesRegex(ValueError, "output_limit"):
            output.write("def")
        self.assertEqual(output.getvalue(), "abcd")

    def test_runtime_pin_cannot_be_replaced(self) -> None:
        pin = self.root / "pin.json"
        pin.write_text(json.dumps({"source_commit": "anything"}))
        with self.assertRaisesRegex(ValueError, "pin_binding"):
            child.verify_pin(pin, self.snapshot, self.root)

    def test_runtime_binary_hash_refuses_and_launch_guard_blocks(self) -> None:
        pin = self.root / "runtime-pin.json"
        raw = json.dumps(
            {
                "source_commit": child.SOURCE_COMMIT,
                "runtime": {
                    "executable": str(Path(sys.executable).resolve()),
                    "executable_sha256": "0" * 64,
                },
            }
        ).encode()
        pin.write_bytes(raw)
        with (
            mock.patch.object(
                child, "PIN_SHA256", hashlib.sha256(raw).hexdigest()
            ),
            self.assertRaisesRegex(ValueError, "runtime_binding"),
        ):
            child.verify_pin(pin, self.snapshot, self.root)
        for event in ("subprocess.Popen", "socket.connect", "os.system"):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                child._deny_launch(event, ())


def fixture_material() -> dict[str, Any]:
    """Build two chronological synthetic cases without runtime seed writes."""
    from_fixture = {
        "timestamp": "2026-01-02T03:04:05Z",
        "type": "session_meta",
        "payload": {
            "id": "baseline-fixture",
            "session_id": "baseline-fixture",
            "cwd": "/synthetic/project",
            "source": "cli",
            "originator": "codex_cli_rs",
            "thread_source": "user",
        },
    }
    records = [from_fixture]
    cases: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for index, message in enumerate(
        ("renew expired credential", "credential renewal confirmed"), 1
    ):
        records.append(
            {
                "timestamp": "2026-01-02T03:04:06Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            }
        )
        content = b"".join(
            json.dumps(r, separators=(",", ":")).encode() + b"\n"
            for r in records
        )
        case_id = f"case-{index}"
        cases.append(
            {
                "id": case_id,
                "family_id": "family",
                "partition": "pilot",
                "chronology": index,
                "evidence": [],
                "later_task": "credential",
                "expected": {
                    "eligible_claim_keys": ["ORACLE_NOT_FOR_BASELINE"],
                    "must_not_save_claim_keys": [],
                    "permitted_scope": {
                        "project_id": "synthetic",
                        "task": None,
                    },
                    "retrieval_claim_keys": [],
                    "later_task_success_rule": None,
                    "labeler_id": "fixture",
                    "label_evidence_paths": ["fixture"],
                },
            }
        )
        sources.append(
            {
                "case_id": case_id,
                "provider": "codex",
                "thread_id": "baseline-fixture",
                "session_root": "baseline-fixture",
                "relative_path": "rollout-fixture.jsonl",
                "content": content.decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "window": {
                    "source": {
                        "provider": "codex",
                        "thread_id": "baseline-fixture",
                    },
                    "from_line": 1,
                    "through_line": len(records),
                    "boundary_hash": hashlib.sha256(
                        content.splitlines()[-1]
                    ).hexdigest(),
                },
                "mutation": "none",
            }
        )
    families = {"family": "synthetic sequential fixture"}
    return {
        "schema_version": 1,
        "synthetic": True,
        "phase": "pilot",
        "families": families,
        "cases": cases,
        "sources": sources,
    }


def fixture_manifest(
    root: Path, source_root: Path, pin_path: Path, boundary: Path
) -> BaselineManifest:
    """Bind the generated fixture to explicit isolated paths and bounds."""
    material = fixture_material()
    families, cases = material["families"], material["cases"]
    material_path = root / "material.json"
    material_path.write_text(json.dumps(material))
    material_path.chmod(0o600)
    value = {
        "schema_version": 1,
        "condition": "capture-only-no-automatic-saver",
        "boundary": str(boundary),
        "source_root": str(source_root),
        "pin_path": str(pin_path),
        "pin_sha256": child.PIN_SHA256,
        "material_path": str(material_path),
        "material_sha256": hashlib.sha256(
            material_path.read_bytes()
        ).hexdigest(),
        "family_digest": digest(families),
        "run_root": str(root / "run"),
        "output_root": str(root / "output"),
        "families": {
            "family": {
                "root": str(root / "run" / "family"),
                "original_cwd": "/synthetic/project",
            }
        },
        "cases": [
            {
                "case_id": c["id"],
                "family_id": "family",
                "chronology": c["chronology"],
                "query": "credential",
            }
            for c in cases
        ],
        "recall_enabled": False,
        "search_limit": 10,
        "input_limit": 65536,
        "output_limit": 65536,
        "timeout_seconds": 10.0,
    }
    return BaselineManifest.model_validate_json(json.dumps(value))


def native_fixture(
    root: Path, source: Path, pin: Path, boundary: Path
) -> dict[str, object]:
    """Run local pinned acceptance with disposable synthetic sources."""
    manifest = fixture_manifest(root, source, pin, boundary)
    result = baseline.run(manifest)
    counts = []
    for row in result["cases"]:
        assert row["code"] == "ok", row
        assert row["summary"]["knowledge_count"] == 0
        assert row["summary"]["prompt_recall_output"] == {}
        assert row["summary"]["session_start_context_bytes"] > 0
        counts.append(len(row["summary"]["transcript_hits"]))
        for operation in row["operations"]:
            assert operation["result"]["module_path"] == str(
                source / "muninn" / "cli.py"
            )
    assert counts == [1, 2], counts
    assert baseline.run(manifest) == result
    return {
        "case_count": len(counts),
        "transcript_hit_counts": counts,
        "knowledge_counts": [0, 0],
        "replay_identical": True,
        "output_root": str(manifest.output_root),
    }


class BaselineFlowTests(unittest.TestCase):
    """Check orchestration and replay using only harmless generated input."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir(mode=0o700)
        self.pin = self.root / "pin.json"
        self.pin.write_bytes(b"{}")
        self.manifest = fixture_manifest(
            self.root, self.snapshot, self.pin, self.root
        )

    def test_bad_material_and_reversed_order_refuse(self) -> None:
        self.manifest.material_path.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "material_binding"):
            models.validate_material(self.manifest)
        manifest = fixture_manifest(
            self.root, self.snapshot, self.pin, self.root
        )
        reversed_manifest = manifest.model_copy(
            update={"cases": tuple(reversed(manifest.cases))}
        )
        with self.assertRaisesRegex(ValueError, "material_schedule_binding"):
            models.validate_material(reversed_manifest)

    def test_scopes_are_mapped_and_oracle_not_rendered(self) -> None:
        material = models.validate_material(self.manifest)
        raw = models.rendered_source(
            material.sources[0], self.manifest.families["family"]
        )
        self.assertIn(str(self.root / "run" / "family" / "cwd").encode(), raw)
        self.assertNotIn(b"ORACLE", raw)
        altered = self.manifest.families["family"].model_copy(
            update={"original_cwd": "/other"}
        )
        with self.assertRaisesRegex(ValueError, "source_scope_binding"):
            models.rendered_source(material.sources[0], altered)

    def test_failure_retains_rows_and_replay_does_not_repeat(self) -> None:
        with (
            mock.patch.object(models, "verify_pin"),
            mock.patch.object(
                baseline, "_operation", side_effect=OSError("fixture")
            ) as operation,
        ):
            result = baseline.run(self.manifest)
            self.assertEqual(operation.call_count, 2)
            restored = baseline.run(self.manifest)
            self.assertEqual(result, restored)
            self.assertEqual(operation.call_count, 2)
        for row in result["cases"]:
            self.assertEqual(row["code"], "failed")
            self.assertEqual(len(row["operations"]), 6)
            self.assertIsNone(row["summary"]["knowledge_count"])
            self.assertEqual(row["error"], "OSError")
        with (
            mock.patch.object(models, "verify_pin"),
            mock.patch.object(baseline, "_operation") as operation,
        ):
            (self.manifest.output_root / "result.json").write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "replay_output_binding"):
                baseline.run(self.manifest)
            operation.assert_not_called()

    def test_fixed_child_timeout_and_output_overflow(self) -> None:
        result = baseline._process(
            sys.executable, b"{}", self.root, 0.000001, 4096
        )
        self.assertEqual(result["error"], "timeout")
        result = baseline._process(sys.executable, b"{}", self.root, 10.0, 4)
        self.assertEqual(result["error"], "output_limit")
        result = baseline._process(
            sys.executable, b'{"operation":"save"}', self.root, 10.0, 4096
        )
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(json.loads(result["stdout"])["code"], "child_refused")

    def test_existing_disposable_root_and_output_alias_refuse(self) -> None:
        self.manifest.run_root.mkdir(mode=0o700)
        with (
            mock.patch.object(models, "verify_pin"),
            self.assertRaisesRegex(ValueError, "disposable_root_exists"),
        ):
            baseline.run(self.manifest)
        self.manifest.run_root.rmdir()
        external = self.root / "external"
        external.mkdir(mode=0o700)
        self.manifest.output_root.symlink_to(
            external, target_is_directory=True
        )
        with self.assertRaises(ValueError):
            baseline.run(self.manifest)
        self.assertEqual(list(external.iterdir()), [])

    def test_material_hash_and_chronology_refuse_before_child(self) -> None:
        material = fixture_material()
        material["sources"][0]["sha256"] = "0" * 64
        path = self.manifest.material_path
        path.write_text(json.dumps(material))
        manifest = self.manifest.model_copy(
            update={
                "material_sha256": hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            }
        )
        with self.assertRaisesRegex(ValueError, "source_binding"):
            models.validate_material(manifest)
        material = fixture_material()
        material["cases"][1]["chronology"] = 0
        path.write_text(json.dumps(material))
        manifest = self.manifest.model_copy(
            update={
                "material_sha256": hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            }
        )
        with self.assertRaisesRegex(ValueError, "partition_leakage"):
            models.validate_material(manifest)
