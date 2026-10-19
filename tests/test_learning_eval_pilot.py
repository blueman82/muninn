"""Drive a serialized synthetic pilot with real capture and fake children."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

from muninn.review_json import result_schema
from muninn.review_output import output_contract_digest
from muninn.review_rounds import round_schema
from muninn.review_runner import config_fingerprint
from tests.classify_support import codex_meta, user_msg
from tests.ingest_support import TID, line, rollout
from tests.test_learning_eval_account import account_assessment
from tools.learning_eval.cases import config_digest, digest, input_digests
from tools.learning_eval.models import Case
from tools.learning_eval.task_protocol import task_schema

PROGRAM = """import json,sys
p=json.load(sys.stdin)
if p['protocol']=='muninn-task-answer-v1':
    available='renew expired credential' in json.dumps(p['knowledge'])
    op={'answer':'renew expired credential' if available else 'unknown'}
else:
    d=p['data']
    if d['round_index']==1:
        op={'op':'search','query':'renew credential','scope':d['scope']}
    else:
        note={'action':'add','form':'note','kind':'procedure',
              'scope':d['scope'],'content':'renew expired credential',
              'conditions':[],'evidence_refs':[d['evidence_ids'][0]],
              'reason':'Owner procedure'}
        op={'op':'final','result':{'schema_version':1,'changes':[note],
            'unresolved':[],'no_change_reason':None}}
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'structured_output':op}))
"""


class PilotFixture(unittest.TestCase):
    """Build private synthetic originals and fake executables."""

    def setUp(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires a POSIX executable fixture")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "disposable"
        self.artifacts = self.root / "artifacts"
        self.account = self.root / "account"
        for path in (self.home, self.artifacts, self.account):
            path.mkdir(mode=0o700)
        self.cwd = self.home / "runner"
        self.cwd.mkdir(mode=0o700)
        self.executable = self.home / "fake-child"
        self.executable.write_text("#!" + sys.executable + "\n" + PROGRAM)
        self.executable.chmod(0o700)
        patch = mock.patch.dict(
            os.environ, {"HOME": str(self.home)}, clear=False
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.config = account_assessment()
        template = {
            "schema_version": 1,
            "assembly": "muninn-review-prompt-v1",
            "instructions": "Extract knowledge from admitted source.",
        }
        self.template = json.dumps(
            template, sort_keys=True, separators=(",", ":")
        ).encode()
        self.prompt_path = self.root / "prompt.json"
        self.prompt_path.write_bytes(self.template)
        self.prompt_path.chmod(0o600)
        policy = self.config.policies[0].model_copy(
            update={
                "runner": self.config.policies[0].runner.model_copy(
                    update={"executable": str(self.executable)}
                ),
                "prompt_digest": hashlib.sha256(self.template).hexdigest(),
                "result_schema_digest": digest(result_schema()),
                "projection_version": "permitted-fields-v2",
                "max_input_bytes": 65536,
                "max_output_bytes": 20000,
                "context_bytes": 12000,
                "deadline_seconds": 20.0,
                "max_rounds": 2,
            }
        )
        self.config = self.config.model_copy(
            update={"policies": (policy,), "sample_size": 1, "repetitions": 1}
        )
        self.material = self.make_material()
        self.material_path = self.root / "pilot.json"
        self.material_path.write_text(json.dumps(self.material))
        self.material_path.chmod(0o600)
        cases = tuple(
            Case.model_validate_json(json.dumps(c))
            for c in self.material["cases"]
        )
        bindings = input_digests(cases)
        bindings["heldout_family_digest"] = digest(
            ["reserved-unopened-family"]
        )
        observed = policy.runner.model_dump(
            mode="json", exclude={"executable"}
        )
        self.config = self.config.model_copy(
            update={
                **bindings,
                "comparator_config_digests": dict.fromkeys(
                    self.config.comparators, digest(observed)
                ),
            }
        )
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )

    def make_material(self) -> dict[str, Any]:
        """Keep the grading marker outside original source and task text."""
        records = [
            codex_meta("user", TID),
            user_msg(1, "renew expired credential"),
        ]
        content = b"".join(line(r) for r in records)
        return {
            "schema_version": 1,
            "synthetic": True,
            "phase": "pilot",
            "families": {"credential": "Synthetic credential procedure"},
            "cases": [
                {
                    "id": "literal",
                    "family_id": "credential",
                    "partition": "pilot",
                    "chronology": 1,
                    "evidence": [],
                    "later_task": "renew credential",
                    "expected": {
                        "eligible_claim_keys": ["SECRET_LABEL"],
                        "must_not_save_claim_keys": [],
                        "permitted_scope": {
                            "project_id": "synthetic",
                            "task": None,
                        },
                        "retrieval_claim_keys": ["SECRET_LABEL"],
                        "later_task_success_rule": "SECRET_RULE",
                        "labeler_id": "independent-labeler",
                        "label_evidence_paths": ["fixture-labels"],
                    },
                }
            ],
            "sources": [
                {
                    "case_id": "literal",
                    "provider": "codex",
                    "thread_id": TID,
                    "session_root": TID,
                    "relative_path": rollout(),
                    "content": content.decode(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "window": {
                        "source": {"provider": "codex", "thread_id": TID},
                        "from_line": 1,
                        "through_line": 2,
                        "boundary_hash": hashlib.sha256(
                            line(records[1]).rstrip(b"\r\n")
                        ).hexdigest(),
                    },
                    "mutation": "none",
                }
            ],
        }

    def binding(self, protocol: str) -> dict[str, Any]:
        """Freeze candidate bytes around one local fake executable."""
        schema = (
            round_schema()
            if protocol == "muninn-review-round-v1"
            else task_schema()
        )
        environment = {
            "HOME": hashlib.sha256(str(self.home).encode()).hexdigest()
        }
        config = config_fingerprint((self.cwd,), environment, self.cwd)
        policy = self.config.policies[0]
        candidate = {
            "schema_version": 1,
            "kind": "synthetic_candidate",
            "recipe": "claude-static-posix-v1",
            "protocol": protocol,
            "assessment_id": self.config.id,
            "budget_account_id": self.config.budget.account_id,
            "requested": policy.runner.model_dump(mode="json"),
            "executable_sha256": hashlib.sha256(
                self.executable.read_bytes()
            ).hexdigest(),
            "schema_digest": digest(schema),
            "output_contract_digest": output_contract_digest(),
            "cli_cwd": str(self.cwd),
            "config_roots": [str(self.cwd)],
            "environment_sha256": environment,
            "config_digest": config,
        }
        path = self.root / (protocol + ".json")
        raw = json.dumps(candidate).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        capability = {
            **policy.runner.model_dump(mode="json"),
            "status": "unverified",
            "proof_path": str(path),
            "schema_digest": digest(schema),
            "config_digest": config,
        }
        return {
            "capability": capability,
            "authorization": {
                "assessment_id": self.config.id,
                "candidate_digest": hashlib.sha256(raw).hexdigest(),
                "config_digest": config,
                "budget": self.config.budget.model_dump(mode="json"),
                "disposable_home": str(self.home),
                "approved_by": self.config.approved_by,
                "approved_at": self.config.approved_at,
            },
        }

    def manifest(self) -> Any:
        """Build the declared population before store creation."""
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.pilot_models"),
            "synthetic pilot contracts are absent",
        )
        module = importlib.import_module("tools.learning_eval.pilot_models")
        learners = {
            c: self.binding("muninn-review-round-v1")
            for c in self.config.comparators
        }
        tasks = {
            c: self.binding("muninn-task-answer-v1")
            for c in self.config.comparators
        }
        data = {
            "schema_version": 1,
            "origin": "synthetic",
            "phase": "pilot",
            "assessment": self.config.model_dump(mode="json"),
            "material_path": str(self.material_path),
            "material_digest": hashlib.sha256(
                self.material_path.read_bytes()
            ).hexdigest(),
            "family_digest": digest(self.material["families"]),
            "reserved_heldout_digest": self.config.heldout_family_digest,
            "learner_prompt_path": str(self.prompt_path),
            "task_instructions": "Answer the task using available context.",
            "learning": learners,
            "task": tasks,
            "account_path": str(self.account),
            "artifact_path": str(self.artifacts),
            "exposures": {
                "literal": {
                    "context_bytes": 10000,
                    "search_limit": 5,
                    "guide_open_limit": 2,
                    "guide_bytes": 10000,
                }
            },
            "judgments": [],
        }
        return module.PilotManifest.model_validate_json(json.dumps(data))


class PilotControllerTests(PilotFixture):
    """Use actual checked learning with no runtime table seeding."""

    def test_end_to_end_capture_read_commit_retrieval_and_task(self) -> None:
        manifest = self.manifest()
        module = importlib.import_module("tools.learning_eval.pilot")
        result = module.run_pilot(manifest, initialize=True)
        self.assertEqual(len(result.trials), 4)
        self.assertEqual(result.model_calls, 6)
        present = next(
            t for t in result.trials if t.comparator == "new_present"
        )
        withheld = next(
            t for t in result.trials if t.comparator == "new_withheld"
        )
        self.assertEqual(present.answer, "renew expired credential")
        self.assertEqual(withheld.answer, "unknown")
        self.assertEqual(present.pair_id, withheld.pair_id)
        self.assertTrue(present.receipts[0].applied)
        self.assertTrue(present.exposure.note_ids)
        self.assertTrue(present.exposure.fts_ids)
        self.assertEqual(withheld.exposure.prompt_context, {})
        self.assertEqual(result.report.status, "unverified")
        for item in (present, withheld):
            with closing(
                sqlite3.connect(item.store_home / "muninn.sqlite")
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM review_call"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM review_receipt"
                    ).fetchone()[0],
                    1,
                )
            for path in item.output_paths:
                raw = path.read_text()
                self.assertNotIn("SECRET_LABEL", raw)
                self.assertNotIn("SECRET_RULE", raw)
        with closing(
            sqlite3.connect(present.store_home / "muninn.sqlite")
        ) as conn:
            generation = conn.execute(
                "SELECT generation FROM review_control"
            ).fetchone()[0]
        again = module.run_pilot(manifest)
        with closing(
            sqlite3.connect(present.store_home / "muninn.sqlite")
        ) as conn:
            self.assertGreater(
                conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0],
                generation,
            )
        self.assertEqual(again.model_calls, result.model_calls)
        self.assertEqual(again.trials, result.trials)

    def test_global_cap_retains_complete_population(self) -> None:
        self.config = self.config.model_copy(
            update={
                "budget": self.config.budget.model_copy(
                    update={"max_model_calls": 1}
                )
            }
        )
        policy = self.config.policies[0].model_copy(
            update={"budget": self.config.budget}
        )
        self.config = self.config.model_copy(update={"policies": (policy,)})
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        module = (
            importlib.import_module("tools.learning_eval.pilot")
            if importlib.util.find_spec("tools.learning_eval.pilot")
            else None
        )
        self.assertIsNotNone(
            module, "complete serialized synthetic pilot controller is absent"
        )
        assert module is not None
        result = module.run_pilot(self.manifest(), initialize=True)
        self.assertEqual(len(result.trials), 4)
        self.assertEqual(result.model_calls, 1)
        self.assertTrue(any("budget" in t.outcome for t in result.trials))
