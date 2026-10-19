"""Retain a real fake-process failure across one explicitly bound successor."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests import test_learning_eval_native_codex as fixtures
from tools.learning_eval.account import Account
from tools.learning_eval.cases import config_digest
from tools.learning_eval.native_codex import (
    create_binding,
    execution_terms,
    native_prompt,
)
from tools.learning_eval.native_predecessor import (
    continuation_history,
    predecessor,
)
from tools.learning_eval.native_terms import (
    account_totals,
    bind_account,
    validate_terms,
)
from tools.learning_eval.pilot import run_pilot
from tools.learning_eval.pilot_models import PilotMaterial, schedule
from tools.learning_eval.task_protocol import protocol_schema


class NativeContinuationTests(unittest.TestCase):
    """Use isolated public Account lifecycle and preserve predecessor bytes."""

    def setUp(self) -> None:
        self.fixture = fixtures.NativeCodexTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.config = f.config.model_copy(
            update={
                "comparators": ("new_present", "new_withheld"),
                "comparator_config_digests": {
                    k: v
                    for k, v in f.config.comparator_config_digests.items()
                    if k in {"new_present", "new_withheld"}
                },
            }
        )
        f.config = f.config.model_copy(
            update={"config_digest": config_digest(f.config)}
        )
        f.executable.write_text(
            "#!" + sys.executable + "\nraise SystemExit(1)\n"
        )
        patch = mock.patch.object(
            f.native, "PINNED_EXECUTABLE_SHA256", self.sha(f.executable)
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.old = f.manifest()
        self.result = run_pilot(self.old, initialize=True)
        self.assertEqual(self.result.cli_launches, 2)
        self.assertEqual(self.result.trials[0].outcome, "worker_failed")
        self.material = PilotMaterial.model_validate_json(
            f.material_path.read_bytes()
        )
        self.old_plans = schedule(self.old, self.material)
        self.directory = f.root / "continuation-schema-v2"
        self.directory.mkdir(mode=0o700)
        for name in ("account", "artifacts", "disposable"):
            (self.directory / name).mkdir(mode=0o700)
        (self.directory / "disposable" / "runner").mkdir(mode=0o700)
        budget = f.config.budget.model_copy(
            update={
                "account_id": "successor",
                "max_model_calls": 48,
                "max_cost_decimal": "240",
            }
        )
        self.config = f.config.model_copy(
            update={
                "id": "successor",
                "budget": budget,
                "policies": (
                    f.config.policies[0].model_copy(update={"budget": budget}),
                ),
            }
        )
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        self.ruling = self.make_ruling()
        self.ruling_path = f.root / "continuation-ruling.json"
        self.write(self.ruling_path, json.dumps(self.ruling).encode())
        self.terms = f.terms.model_copy(
            update={
                "assessment_id": "successor",
                "account_id": "successor",
                "account_path": self.directory / "account",
                "receipt_path": self.directory / "receipt.json",
                "carried_launches": 2,
                "carried_charge": "10",
                "predecessor_path": self.ruling_path,
                "predecessor_digest": self.sha(self.ruling_path),
            }
        )
        self.original_bytes = {
            Path(item["path"]): Path(item["path"]).read_bytes()
            for item in self.ruling["evidence"].values()
        }

    def sha(self, path: Path) -> str:
        """Digest exact private evidence bytes."""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write(self, path: Path, raw: bytes) -> None:
        """Create a private artificial authority artifact."""
        path.write_bytes(raw)
        path.chmod(0o600)

    def make_ruling(self) -> dict[str, Any]:
        """Bind the stopped pipeline's actual output and Account bytes."""
        f = self.fixture
        paths = {
            "account": f.account / "account.sqlite3",
            "import_receipt": f.terms.receipt_path,
            "original_canary": f.terms.carry_in_path,
            "failed_output": self.result.trials[0].output_paths[0],
        }
        for name, value in (
            ("manifest", self.old.model_dump(mode="json")),
            ("result", self.result.model_dump(mode="json")),
            (
                "predeclared_schedule",
                [p.model_dump(mode="json") for p in self.old_plans],
            ),
        ):
            paths[name] = f.root / ("prior-" + name + ".json")
            self.write(paths[name], json.dumps(value).encode())
        return {
            "evidence": {
                k: {
                    "path": str(p),
                    "sha256": self.sha(p),
                    "bytes": p.stat().st_size,
                }
                for k, p in paths.items()
            },
            "facts": {
                "started_at_utc": datetime.fromtimestamp(
                    f.terms.started_at, UTC
                ).isoformat(),
                "total_spent_cli_launches": 2,
                "retained_reservations_usd": "10",
                "actual_model_requests": None,
                "actual_cost_usd": None,
            },
            "successor": {
                "assessment_id": "successor",
                "account_id": "successor",
                "subdirectory": str(self.directory),
                "carried_launches": 2,
                "carried_reservations_usd": "10",
                "max_additional_launches": 48,
                "max_remaining_reservations_usd": "240",
                "new_trials": len(self.old_plans),
                "same_population": True,
            },
        }

    def successor(self) -> Any:
        """Construct fresh transport bindings without replacing prior files."""
        f = self.fixture
        f.executable.write_text(
            "#!" + sys.executable + "\n" + fixtures.CODEX_PROGRAM
        )
        patch = mock.patch.object(
            f.native, "PINNED_EXECUTABLE_SHA256", self.sha(f.executable)
        )
        patch.start()
        self.addCleanup(patch.stop)
        bindings = {}
        home = self.directory / "disposable"
        for purpose, protocol in (
            ("learning", "muninn-review-round-v1"),
            ("task", "muninn-task-answer-v1"),
        ):
            binding = create_binding(
                self.config,
                self.terms,
                home,
                home / "runner",
                f.auth_home,
                self.directory / (purpose + ".json"),
                protocol,
            )
            bindings[purpose] = dict.fromkeys(self.config.comparators, binding)
        return self.old.model_copy(
            update={
                "assessment": self.config,
                "native_terms": self.terms,
                "account_path": self.directory / "account",
                "artifact_path": self.directory / "artifacts",
                **bindings,
            }
        )

    def test_verified_continuation_preserves_history_and_remaining_charge(
        self,
    ) -> None:
        manifest = self.successor()
        prior, mapping = continuation_history(
            manifest, schedule(manifest, self.material)
        )
        self.assertEqual(prior, self.result)
        self.assertEqual(len(mapping), len(self.old_plans))
        result = run_pilot(manifest, initialize=True)
        self.assertEqual(result.cli_launches, 8)
        self.assertEqual(result.reserved_cost_decimal, "40")
        self.assertEqual(result.predecessor_trials, self.result.trials)
        self.assertEqual(len(result.trials), len(self.result.trials))
        self.assertEqual(result.predecessor_trial_map, mapping)
        self.assertIn(
            "correction_rerun_same_population_predecessor_failed_history_retained",
            result.report.limitations,
        )
        self.assertEqual(run_pilot(manifest), result)
        for path, raw in self.original_bytes.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_receipt_import_is_idempotent_and_budget_cannot_reset(
        self,
    ) -> None:
        with Account(
            self.terms.account_path, self.config, initialize=True
        ) as account:
            bind_account(account, self.terms, time.time())
            bind_account(account, self.terms, time.time())
            self.assertEqual(account_totals(account, self.terms), (2, "10"))
        with Account(self.terms.account_path, self.config) as account:
            bind_account(account, self.terms, time.time())
        for carried, charge, start in (
            (1, "5", self.terms.started_at),
            (2, "5", self.terms.started_at),
            (2, "10", self.terms.started_at + 1),
        ):
            with (
                self.subTest(carried=carried, charge=charge, start=start),
                self.assertRaises(ValueError),
            ):
                validate_terms(
                    self.terms.model_copy(
                        update={
                            "carried_launches": carried,
                            "carried_charge": charge,
                            "started_at": start,
                        }
                    ),
                    self.config,
                    time.time(),
                )
        widened = self.config.model_copy(
            update={
                "budget": self.config.budget.model_copy(
                    update={"max_model_calls": 49}
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "native_budget_binding"):
            validate_terms(self.terms, widened, time.time())
        (self.terms.account_path / "account.sqlite3").unlink()
        with (
            Account(
                self.terms.account_path, self.config, initialize=True
            ) as account,
            self.assertRaisesRegex(ValueError, "native_account_reset"),
        ):
            bind_account(account, self.terms, time.time())

    def test_changed_evidence_and_population_refuse(self) -> None:
        for path, raw in self.original_bytes.items():
            with self.subTest(path=path):
                path.write_bytes(raw + b" ")
                with self.assertRaises(ValueError):
                    predecessor(self.terms, self.config)
                path.write_bytes(raw)
        manifest = self.successor()
        changed = manifest.model_copy(update={"material_digest": "0" * 64})
        with self.assertRaisesRegex(
            ValueError, "native_predecessor_population"
        ):
            continuation_history(changed, schedule(manifest, self.material))
        with self.assertRaisesRegex(
            ValueError, "native_predecessor_population"
        ):
            continuation_history(
                manifest, schedule(manifest, self.material)[:-1]
            )

    def test_domain_guidance_and_actual_bound_are_program_owned(self) -> None:
        raw = json.dumps(
            {"instructions": self.fixture.native.JSON_ONLY_INSTRUCTION}
        ).encode()
        for protocol in ("muninn-review-round-v1", "muninn-task-answer-v1"):
            prompt = native_prompt(raw, protocol, 65536)
            self.assertEqual(
                json.loads(prompt)["payload_schema"], protocol_schema(protocol)
            )
            with self.assertRaisesRegex(ValueError, "invalid_prompt_size"):
                native_prompt(raw, protocol, len(prompt) - 1)
            other = (
                "muninn-task-answer-v1"
                if protocol == "muninn-review-round-v1"
                else "muninn-review-round-v1"
            )
            with self.assertRaises(ValueError):
                self.fixture.native.validate_native_prompt(prompt, other)

    def test_rehashed_omitted_or_uncleaned_claim_still_refuses(self) -> None:
        path = Path(self.ruling["evidence"]["account"]["path"])
        original = path.read_bytes()
        for omission in (False, True):
            with closing(sqlite3.connect(":memory:")) as conn:
                conn.deserialize(original)
                if omission:
                    conn.execute("DELETE FROM calls")
                else:
                    record = json.loads(
                        conn.execute("SELECT record FROM calls").fetchone()[0]
                    )
                    record["cleaned"] = False
                    conn.execute(
                        "UPDATE calls SET record=?", (json.dumps(record),)
                    )
                conn.commit()
                path.write_bytes(conn.serialize())
            self.ruling["evidence"]["account"]["sha256"] = self.sha(path)
            self.ruling["evidence"]["account"]["bytes"] = path.stat().st_size
            self.write(self.ruling_path, json.dumps(self.ruling).encode())
            terms = self.terms.model_copy(
                update={"predecessor_digest": self.sha(self.ruling_path)}
            )
            with (
                self.subTest(omission=omission),
                self.assertRaisesRegex(
                    ValueError, "native_predecessor_binding"
                ),
            ):
                predecessor(terms, self.config)
        path.write_bytes(original)

    def test_correction_stops_at_eighteen_new_claims(self) -> None:
        manifest = self.successor()
        plan = schedule(manifest, self.material)[0]
        binding = manifest.learning[plan.comparator]
        with Account(
            self.terms.account_path, self.config, initialize=True
        ) as account:
            bind_account(account, self.terms, time.time())
            for index in range(18):
                call = plan.calls[0].model_copy(
                    update={
                        "id": "A_" + format(index, "032x"),
                        "trial_id": "fixture-" + str(index),
                    }
                )
                account.claim(call, binding.authorization, time.time())
                account.finish(call.id, None, "fixture_contained", time.time())
            self.assertEqual(account_totals(account, self.terms), (20, "100"))
            with self.assertRaisesRegex(ValueError, "budget_call_limit"):
                execution_terms(
                    account,
                    binding.capability,
                    "A_" + "f" * 32,
                    binding.authorization.candidate_digest,
                )
