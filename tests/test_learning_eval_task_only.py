"""Exercise paired task-only transport without new learning authority."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn.review_json import canonical_json
from tests import test_learning_eval_native_codex as native_fixture
from tests import (
    test_learning_eval_native_continuation as continuation_fixture,
)
from tests.classify_support import codex_meta, user_msg
from tests.ingest_support import line, rollout
from tools.learning_eval.cases import config_digest, digest, input_digests
from tools.learning_eval.models import Case
from tools.learning_eval.pilot import run_pilot


class TaskOnlyTests(unittest.TestCase):
    """Use real fake-child calls and completed public learner state."""

    def setUp(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.task_only")
        )
        self.module = importlib.import_module("tools.learning_eval.task_only")
        self.models = importlib.import_module(
            "tools.learning_eval.task_only_models"
        )
        original_setup = native_fixture.NativeCodexTests.setUp
        original_manifest = native_fixture.NativeCodexTests.manifest

        def setup(f: Any) -> None:
            original_setup(f)
            self.expand(f)

        def manifest(f: Any) -> Any:
            result = original_manifest(f)
            return result.model_copy(
                update={
                    "exposures": dict.fromkeys(
                        ("literal", "paraphrase", "omitted"),
                        result.exposures["literal"],
                    )
                }
            )

        program = (
            native_fixture.CODEX_PROGRAM.replace(
                "if d['round_index']==1:", "if False:"
            )
            .replace("'fixture-model'", "'gpt-6.1-sol'")
            .replace(
                "model_reasoning_effort=high", "model_reasoning_effort=low"
            )
        )
        program = program.replace(
            "assert schema==",
            "if p.get('data') and 'omit learner change' in json.dumps(p):\n"
            "    op={'op':'final','result':{'schema_version':1,'changes':[],"
            "'unresolved':[],'no_change_reason':'No new durable claim.'}}\n"
            "assert schema==",
        )
        for patch in (
            mock.patch.object(native_fixture.NativeCodexTests, "setUp", setup),
            mock.patch.object(
                native_fixture.NativeCodexTests, "manifest", manifest
            ),
            mock.patch.object(native_fixture, "CODEX_PROGRAM", program),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.fixture = continuation_fixture.NativeContinuationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.pilot = self.fixture.successor()
        self.result = run_pilot(self.pilot, initialize=True)
        self.assertEqual(self.result.cli_launches, 14)
        self.assertEqual(len(self.result.trials), 6)
        self.root = self.fixture.directory
        self.manifest_path = self.root / "completed-manifest.json"
        self.result_path = self.root / "completed-result.json"
        for path, value in (
            (self.manifest_path, self.pilot),
            (self.result_path, self.result),
        ):
            path.write_text(canonical_json(value))
            path.chmod(0o600)
        self.output = self.root / "task-only"
        self.output.mkdir(mode=0o700)
        self.manifest = self.module.prepare_task_only(
            self.ref(self.manifest_path),
            self.ref(self.result_path),
            self.output,
        )
        self.original = {self.result_path: self.result_path.read_bytes()}
        self.original.update(
            {p: p.read_bytes() for p in self.fixture.original_bytes}
        )
        self.stores = {
            t.store_home
            / "muninn.sqlite": (t.store_home / "muninn.sqlite").read_bytes()
            for t in self.result.trials
        }

    def ref(self, path: Path) -> Any:
        """Bind a private artifact's exact bytes."""
        return self.models.ArtifactRef(
            path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def expand(self, f: Any) -> None:
        """Make three synthetic cases with one-round completed learning."""
        cases, sources = [], []
        for index, name in enumerate(("literal", "paraphrase", "omitted"), 1):
            case = copy.deepcopy(f.material["cases"][0])
            case.update(id=name, chronology=index)
            tid = "00000000-0000-0000-0000-" + format(index, "012d")
            records = [
                codex_meta("user", tid),
                user_msg(
                    1,
                    (
                        "omit learner change"
                        if name == "omitted"
                        else "renew expired credential"
                    ),
                ),
            ]
            content = b"".join(line(r) for r in records)
            source = copy.deepcopy(f.material["sources"][0])
            source.update(
                case_id=name,
                thread_id=tid,
                session_root=tid,
                relative_path=rollout(tid),
                content=content.decode(),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            source["window"].update(
                source={"provider": "codex", "thread_id": tid},
                boundary_hash=hashlib.sha256(
                    line(records[1]).rstrip(b"\r\n")
                ).hexdigest(),
            )
            cases.append(case)
            sources.append(source)
        f.material.update(cases=cases, sources=sources)
        f.material_path.write_text(json.dumps(f.material))
        policy = f.config.policies[0]
        policy = policy.model_copy(
            update={
                "deadline_seconds": 120.0,
                "max_output_bytes": 32768,
                "runner": policy.runner.model_copy(
                    update={"model": "gpt-6.1-sol", "reasoning": "low"}
                ),
            }
        )
        f.config = f.config.model_copy(
            update={
                **input_digests(
                    tuple(
                        Case.model_validate_json(json.dumps(c)) for c in cases
                    )
                ),
                "sample_size": 3,
                "policies": (policy,),
                "comparator_config_digests": dict.fromkeys(
                    f.config.comparators,
                    digest(
                        policy.runner.model_dump(
                            mode="json", exclude={"executable"}
                        )
                    ),
                ),
            }
        )
        f.config = f.config.model_copy(
            update={"config_digest": config_digest(f.config)}
        )

    def test_same_state_six_calls_replay_and_no_store_writes(self) -> None:
        with mock.patch(
            "tools.learning_eval.new_muninn.learn",
            side_effect=AssertionError("relearning"),
        ):
            result = self.module.run_task_only(self.manifest)
            self.assertEqual(self.module.run_task_only(self.manifest), result)
        self.assertEqual(result.incremental_cli_launches, 6)
        self.assertEqual(result.cumulative_cli_launches, 20)
        self.assertEqual(result.cumulative_reserved_cost_decimal, "100")
        self.assertEqual(len(result.rows), 6)
        for left, right in zip(
            result.rows[::2], result.rows[1::2], strict=True
        ):
            self.assertEqual(
                left.plan.learned_digest, right.plan.learned_digest
            )
            self.assertEqual(
                left.plan.exposure_digest, right.plan.exposure_digest
            )
            self.assertEqual(left.answer, "renew expired credential")
            self.assertEqual(right.answer, "unknown")
            self.assertEqual(left.plan.schedule.purpose, "task")
            for row in (left, right):
                saved = json.loads(row.output_path.read_bytes())
                self.assertEqual(saved["input_digest"], row.plan.prompt_digest)
        for path, raw in {**self.original, **self.stores}.items():
            self.assertEqual(path.read_bytes(), raw)
        output = result.rows[0].output_path
        raw = output.read_bytes()
        for changed in (None, b"corrupted"):
            if changed is None:
                output.unlink()
            else:
                output.write_bytes(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.module.run_task_only(self.manifest)
            output.write_bytes(raw)
            output.chmod(0o600)

    def test_changed_sources_mapping_schedule_refuse_before_claim(
        self,
    ) -> None:
        before = (self.pilot.account_path / "account.sqlite3").read_bytes()
        for path in (
            self.result_path,
            self.manifest.sources[0].mapping.path,
            self.output / "schedule.json",
        ):
            raw = path.read_bytes()
            path.write_bytes(raw + b" ")
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.module.run_task_only(self.manifest)
            path.write_bytes(raw)
        self.assertEqual(
            (self.pilot.account_path / "account.sqlite3").read_bytes(), before
        )

    def test_native_failure_stops_remaining_rows_and_replay(self) -> None:
        executable = Path(self.pilot.assessment.policies[0].runner.executable)
        # Inject decoder failure without changing selected executable bytes.
        from_bytes = executable.read_bytes()
        create = self.module.execute_result

        def fail(*args: Any, **kwargs: Any) -> Any:
            with mock.patch(
                "muninn.review_process.decode_codex_output",
                side_effect=ValueError("invalid_native_output"),
            ):
                return create(*args, **kwargs)

        with mock.patch.object(self.module, "execute_result", fail):
            result = self.module.run_task_only(self.manifest)
        self.assertEqual(result.incremental_cli_launches, 1)
        self.assertEqual(len(result.rows), 6)
        self.assertTrue(all(r.outcome != "completed" for r in result.rows))
        self.assertEqual(self.module.run_task_only(self.manifest), result)
        self.assertEqual(executable.read_bytes(), from_bytes)

    def test_reset_account_and_shifted_authority_refuse(self) -> None:
        database = self.pilot.account_path / "account.sqlite3"
        raw = database.read_bytes()
        database.write_bytes(raw + b" ")
        with self.assertRaises(ValueError):
            self.module.run_task_only(self.manifest)
        database.write_bytes(raw)
        bad = self.manifest.model_copy(update={"account_preimage": "0" * 64})
        with self.assertRaises(ValueError):
            self.module.run_task_only(bad)

    def test_refused_row_cannot_revive_without_a_spent_claim(self) -> None:
        with mock.patch.object(
            self.module,
            "execute_result",
            side_effect=ValueError("configuration_refused"),
        ):
            result = self.module.run_task_only(self.manifest)
        self.assertEqual(result.incremental_cli_launches, 0)
        self.assertEqual(len(result.rows), 6)
        with mock.patch.object(
            self.module,
            "execute_result",
            side_effect=AssertionError("revived refusal"),
        ):
            self.assertEqual(self.module.run_task_only(self.manifest), result)

    def test_expired_original_clock_refuses_before_claim(self) -> None:
        before = (self.pilot.account_path / "account.sqlite3").read_bytes()
        with (
            mock.patch(
                "tools.learning_eval.capability.time.time",
                return_value=self.pilot.native_terms.started_at + 14401,
            ),
            self.assertRaisesRegex(ValueError, "budget_wall_limit"),
        ):
            self.module.run_task_only(self.manifest)
        self.assertEqual(
            (self.pilot.account_path / "account.sqlite3").read_bytes(), before
        )

    def test_invalid_answer_retains_observed_usage_and_stops(self) -> None:
        decoder = importlib.import_module(
            "muninn.review_codex_output"
        ).decode_codex_output

        def wrong_shape(raw: bytes, limit: int) -> Any:
            _, observed, usage = decoder(raw, limit)
            return b"{}", observed, usage

        with mock.patch(
            "muninn.review_process.decode_codex_output", wrong_shape
        ):
            result = self.module.run_task_only(self.manifest)
        self.assertEqual(result.incremental_cli_launches, 1)
        self.assertEqual(result.rows[0].outcome, "invalid_task_answer")
        self.assertEqual(result.rows[0].usage.input_tokens, 10)
        self.assertIsNotNone(result.rows[0].elapsed_seconds)
        self.assertIsNotNone(result.rows[0].output_path)

    def test_spent_failure_without_output_replays_terminal_rows(self) -> None:
        with mock.patch(
            "tools.learning_eval.execution.create_wrapper",
            side_effect=OSError("fixture_containment_uncertain"),
        ):
            first = self.module.run_task_only(self.manifest)
        self.assertEqual(first.incremental_cli_launches, 1)
        self.assertEqual(len(first.rows), 6)
        self.assertEqual(
            first.rows[0].outcome, "fixture_containment_uncertain"
        )
        with mock.patch.object(
            self.module,
            "execute_result",
            side_effect=AssertionError("unexpected relaunch"),
        ):
            self.assertEqual(self.module.run_task_only(self.manifest), first)
