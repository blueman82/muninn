"""Run a fake Codex process through real capture, learning and task calls."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import time
from typing import Any
from unittest import mock

from tests.test_learning_eval_native_terms import terms_data
from tests.test_learning_eval_pilot import PROGRAM, PilotFixture
from tools.learning_eval.cases import config_digest, digest

CODEX_PROGRAM = (
    """import json,os,sys
from pathlib import Path
args=sys.argv[1:]
assert args[:10]==['exec','--ephemeral','--ignore-user-config',
    '--ignore-rules',
    '--disable','hooks','--sandbox','read-only','--skip-git-repo-check','-C']
assert args[10]==os.getcwd()
assert args[11]=='--output-schema'
schema=json.loads(Path(args[12]).read_text())
assert args[13:]==['--json','--color','never','--model','fixture-model',
    '-c','model_reasoning_effort=high','-']
assert os.environ['HOME']!=os.environ['CODEX_HOME']
assert set(os.environ)<= {'HOME','CODEX_HOME','PATH','LC_CTYPE',
    '__CF_USER_TEXT_ENCODING'}
"""
    + PROGRAM[PROGRAM.index("p=json.load") : PROGRAM.index("print(json.dumps")]
    + """
assert schema=={'type':'object',
    'properties':{'payload_json':{'type':'string'}},
    'required':['payload_json'],'additionalProperties':False}
assert 'payload_json' in p['instructions']
assert ('answer' in op)==('answer' in p['payload_schema'].get('properties',{}))
for e in [{'type':'thread.started','thread_id':'fixture'},
    {'type':'turn.started'},
    {'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':json.dumps({'payload_json':json.dumps(op)})}},
    {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}]:
    print(json.dumps(e))
"""
)


class NativeCodexTests(PilotFixture):
    """Use only artificial auth paths, authority and executable bytes."""

    def setUp(self) -> None:
        super().setUp()
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.native_codex")
        )
        self.native = importlib.import_module(
            "tools.learning_eval.native_codex"
        )
        for name, value in (("system", "Darwin"), ("machine", "arm64")):
            host = mock.patch.object(
                self.native.platform, name, return_value=value
            )
            host.start()
            self.addCleanup(host.stop)
        self.terms_module = importlib.import_module(
            "tools.learning_eval.native_terms"
        )
        template = json.loads(self.template)
        template["instructions"] += " " + self.native.JSON_ONLY_INSTRUCTION
        self.template = json.dumps(template).encode()
        self.prompt_path.write_bytes(self.template)
        self.executable.write_text(
            "#!" + sys.executable + "\n" + CODEX_PROGRAM
        )
        self.auth_home = self.root / "auth"
        self.auth_home.mkdir(mode=0o700)
        budget = self.config.budget.model_copy(
            update={
                "max_model_calls": 49,
                "max_cost_decimal": "245",
                "conservative_call_cost_decimal": "5",
                "max_wall_seconds": 14400.0,
            }
        )
        policy = self.config.policies[0]
        policy = policy.model_copy(
            update={
                "budget": budget,
                "prompt_digest": hashlib.sha256(self.template).hexdigest(),
                "max_attempts": 1,
                "runner": policy.runner.model_copy(
                    update={
                        "runner": "codex_cli",
                        "version": "0.163.0",
                        "model": "fixture-model",
                        "reasoning": "high",
                    }
                ),
            }
        )
        self.config = self.config.model_copy(
            update={
                "budget": budget,
                "policies": (policy,),
                "comparator_config_digests": dict.fromkeys(
                    self.config.comparators,
                    digest(
                        policy.runner.model_dump(
                            mode="json", exclude={"executable"}
                        )
                    ),
                ),
            }
        )
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        self.terms = self.terms_module.NativeTerms.model_validate_json(
            json.dumps(terms_data(self.root, self.config, time.time() - 10))
        )
        patch = mock.patch.object(
            self.native,
            "PINNED_EXECUTABLE_SHA256",
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def binding(self, protocol: str) -> dict[str, Any]:
        """Construct immutable native bindings before any account claim."""
        return self.native.create_binding(
            self.config,
            self.terms,
            self.home,
            self.cwd,
            self.auth_home,
            self.root / (protocol + ".json"),
            protocol,
        ).model_dump(mode="json")

    def manifest(self) -> Any:
        """Select native output over declared synthetic sources."""
        result = super().manifest()
        return result.model_copy(
            update={
                "origin": "native_codex_trial",
                "native_terms": self.terms,
                "task_instructions": result.task_instructions
                + " "
                + self.native.JSON_ONLY_INSTRUCTION,
            }
        )

    def test_real_pipeline_and_replay_preserve_canary(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        result = module.run_pilot(self.manifest(), initialize=True)
        self.assertEqual(result.origin, "native_codex_trial")
        self.assertEqual(result.cli_launches, 7)
        self.assertIsNone(result.model_calls)
        self.assertEqual(result.reserved_cost_decimal, "35")
        self.assertEqual(len(result.trials), 4)
        present = next(
            t for t in result.trials if t.comparator == "new_present"
        )
        withheld = next(
            t for t in result.trials if t.comparator == "new_withheld"
        )
        self.assertEqual(present.answer, "renew expired credential")
        self.assertEqual(withheld.answer, "unknown")
        self.assertTrue(present.receipts[0].applied)
        self.assertTrue(present.exposure.note_ids)
        self.assertTrue(present.exposure.fts_ids)
        self.assertEqual(withheld.exposure.prompt_context, {})
        self.assertIn(
            "cli_launch_accounting_only_internal_model_requests_unknown",
            result.report.limitations,
        )
        self.assertNotIn(
            "synthetic_harness_not_native_proof", result.report.limitations
        )
        again = module.run_pilot(self.manifest())
        self.assertEqual(again.cli_launches, 7)
        self.assertEqual(again.trials, result.trials)

    def test_tool_event_stops_all_later_launches_and_reports_failure(
        self,
    ) -> None:
        bad = CODEX_PROGRAM.replace(
            "'type':'agent_message'", "'type':'command_execution'"
        )
        self.executable.write_text("#!" + sys.executable + "\n" + bad)
        with mock.patch.object(
            self.native,
            "PINNED_EXECUTABLE_SHA256",
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        ):
            result = importlib.import_module(
                "tools.learning_eval.pilot"
            ).run_pilot(self.manifest(), initialize=True)
        self.assertEqual(result.cli_launches, 2)
        self.assertEqual(len(result.trials), 4)
        self.assertIn("native_tool_event", [t.outcome for t in result.trials])
        self.assertIn(
            "observed_native_tool_event_stopped_trial",
            result.report.limitations,
        )
        self.assertTrue(
            any(
                t.outcome == "native_trial_stopped_after_tool_event"
                for t in result.trials
            )
        )

    def test_candidate_drift_refuses_before_account_creation(self) -> None:
        manifest = self.manifest()
        run = importlib.import_module("tools.learning_eval.pilot").run_pilot
        paths = [
            self.executable,
            self.terms.owner_boundary_path,
            self.terms.carry_in_path,
            self.home / "muninn-review-round-v1.schema.json",
            manifest.learning["new_present"].capability.proof_path,
        ]
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.name):
                try:
                    path.write_bytes(original + b" ")
                    with self.assertRaises(ValueError):
                        run(manifest, initialize=True)
                    self.assertFalse(
                        (self.account / "account.sqlite3").exists()
                    )
                finally:
                    path.write_bytes(original)
        for changes in ({"origin": "synthetic"}, {"native_terms": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                run(manifest.model_copy(update=changes), initialize=True)
        for changes in ({"model": "another-model"}, {"reasoning": "low"}):
            policy = manifest.assessment.policies[0]
            policy = policy.model_copy(
                update={"runner": policy.runner.model_copy(update=changes)}
            )
            assessment = manifest.assessment.model_copy(
                update={"policies": (policy,)}
            )
            assessment = assessment.model_copy(
                update={"config_digest": config_digest(assessment)}
            )
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                run(
                    manifest.model_copy(update={"assessment": assessment}),
                    initialize=True,
                )
        self.assertFalse((self.account / "account.sqlite3").exists())

    def test_missing_authority_and_wrong_host_refuse(self) -> None:
        manifest = self.manifest()
        run = importlib.import_module("tools.learning_eval.pilot").run_pilot
        with (
            mock.patch(
                "tools.learning_eval.native_terms.host_identity",
                return_value="a" * 64,
            ),
            self.assertRaisesRegex(ValueError, "native_terms_binding"),
        ):
            run(manifest, initialize=True)
        self.terms.carry_in_path.unlink()
        with self.assertRaises((ValueError, OSError)):
            run(manifest, initialize=True)
        self.assertFalse((self.account / "account.sqlite3").exists())

    def test_tool_event_before_failed_or_hanging_turn_stops_immediately(
        self,
    ) -> None:
        bad = CODEX_PROGRAM.replace(
            "'type':'agent_message'", "'type':'command_execution'"
        )
        bad = bad.replace(
            "print(json.dumps(e))",
            "print(json.dumps(e),flush=True)\n"
            "    if e['type']=='item.completed':\n"
            "        __import__('time').sleep(10)",
        )
        self.executable.write_text("#!" + sys.executable + "\n" + bad)
        began = time.monotonic()
        with mock.patch.object(
            self.native,
            "PINNED_EXECUTABLE_SHA256",
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        ):
            result = importlib.import_module(
                "tools.learning_eval.pilot"
            ).run_pilot(self.manifest(), initialize=True)
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(result.cli_launches, 2)
        self.assertIn("native_tool_event", [t.outcome for t in result.trials])

    def test_prompt_missing_json_only_instruction_refuses(self) -> None:
        manifest = self.manifest()
        manifest = manifest.model_copy(
            update={"task_instructions": "Do anything"}
        )
        run = importlib.import_module("tools.learning_eval.pilot").run_pilot
        with self.assertRaisesRegex(ValueError, "native_prompt_instruction"):
            run(manifest, initialize=True)
        self.assertFalse((self.account / "account.sqlite3").exists())

    def test_timeout_stops_population_without_retry(self) -> None:
        policy = self.config.policies[0].model_copy(
            update={"deadline_seconds": 1.0}
        )
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        bad = "import time; time.sleep(3)\n" + CODEX_PROGRAM
        self.executable.write_text("#!" + sys.executable + "\n" + bad)
        with mock.patch.object(
            self.native,
            "PINNED_EXECUTABLE_SHA256",
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        ):
            result = importlib.import_module(
                "tools.learning_eval.pilot"
            ).run_pilot(self.manifest(), initialize=True)
        self.assertEqual(result.cli_launches, 2)
        self.assertIn("deadline", [t.outcome for t in result.trials])
        self.assertTrue(
            any(
                t.outcome == "native_trial_stopped_after_timeout"
                for t in result.trials
            )
        )

    def failed_stream(self, output: bytes) -> Any:
        """Exercise native containment with exact untrusted stdout bytes."""
        code = "import sys,time\nsys.stdout.buffer.write(" + repr(output)
        code += ");sys.stdout.flush();time.sleep(.1)\n"
        self.executable.write_text("#!" + sys.executable + "\n" + code)
        with mock.patch.object(
            self.native,
            "PINNED_EXECUTABLE_SHA256",
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
        ):
            return importlib.import_module(
                "tools.learning_eval.pilot"
            ).run_pilot(self.manifest(), initialize=True)

    def test_tool_in_limit_crossing_chunk_stops_and_remains_visible(
        self,
    ) -> None:
        event = {
            "type": "item.started",
            "item": {
                "id": "x",
                "type": "command_execution",
                "command": "true",
            },
        }
        result = self.failed_stream(
            b" " * 19990 + b"\n" + json.dumps(event).encode() + b"\n"
        )
        self.assertEqual(result.cli_launches, 2)
        self.assertIn("native_tool_event", [t.outcome for t in result.trials])
        self.assertIn(
            "observed_native_tool_event_stopped_trial",
            result.report.limitations,
        )

    def test_unrecognized_overflow_stops_without_inventing_tool_event(
        self,
    ) -> None:
        result = self.failed_stream(b"x" * 20100)
        self.assertEqual(result.cli_launches, 2)
        self.assertIn("output_limit", [t.outcome for t in result.trials])
        self.assertIn(
            "native_trial_stopped_after_uncertain_output",
            [t.outcome for t in result.trials],
        )
        self.assertNotIn(
            "observed_native_tool_event_stopped_trial",
            result.report.limitations,
        )

    def test_truncated_native_output_stops_without_inventing_tool_event(
        self,
    ) -> None:
        result = self.failed_stream(
            b'{"type":"item.started","item":{"type":"command_'
        )
        self.assertEqual(result.cli_launches, 2)
        self.assertIn(
            "invalid_native_output", [t.outcome for t in result.trials]
        )
        self.assertIn(
            "native_trial_stopped_after_uncertain_output",
            [t.outcome for t in result.trials],
        )
        self.assertNotIn(
            "observed_native_tool_event_stopped_trial",
            result.report.limitations,
        )
