"""Exercise checked pilot failures and lifecycle boundaries."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import store
from muninn.review_process import WorkerReady
from muninn.review_supervisor import Supervisor
from tests.test_learning_eval_pilot import PilotFixture
from tools.learning_eval import execution, pilot_bridge, pilot_sources
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest
from tools.learning_eval.models import IndependentJudgment
from tools.learning_eval.pilot_models import JudgmentBinding


class NewMuninnPilotTests(PilotFixture):
    """Require real lifecycle and immutable evidence across replay."""

    def test_labels_never_enter_actual_prompts_and_no_supervisor_scheduler(
        self,
    ) -> None:
        prompts: list[bytes] = []
        original = execution.create_wrapper

        def capture(spec: Any, *args: Any, **kwargs: Any) -> Any:
            prompts.append(spec.prompt)
            return original(spec, *args, **kwargs)

        with (
            mock.patch.object(
                execution, "create_wrapper", side_effect=capture
            ),
            mock.patch.object(
                Supervisor,
                "tick",
                side_effect=AssertionError("scheduler called"),
            ),
            mock.patch.object(
                Supervisor,
                "_spawn",
                side_effect=AssertionError("spawn called"),
            ),
        ):
            module = importlib.import_module("tools.learning_eval.pilot")
            result = module.run_pilot(self.manifest(), initialize=True)
        self.assertEqual(result.model_calls, 6)
        self.assertEqual(len(prompts), 6)
        self.assertTrue(
            all(
                b"SECRET_LABEL" not in p and b"SECRET_RULE" not in p
                for p in prompts
            )
        )
        task = [
            json.loads(p)
            for p in prompts
            if json.loads(p)["protocol"] == "muninn-task-answer-v1"
        ]
        self.assertEqual(task[1]["knowledge"], {})

    def test_missing_saved_output_is_unavailable_without_relaunch(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        first = module.run_pilot(manifest, initialize=True)
        present = next(
            t for t in first.trials if t.comparator == "new_present"
        )
        present.output_paths[-1].unlink()
        with mock.patch.object(
            execution, "create_wrapper", side_effect=AssertionError("relaunch")
        ):
            second = module.run_pilot(manifest)
        self.assertEqual(second.model_calls, 6)
        self.assertEqual(
            next(
                t for t in second.trials if t.comparator == "new_present"
            ).outcome,
            "unavailable_evidence",
        )

    def test_edited_answer_checkpoint_cannot_replace_saved_output(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        first = module.run_pilot(manifest, initialize=True)
        present = next(
            t for t in first.trials if t.comparator == "new_present"
        )
        path = self.artifacts / (present.id + ".json")
        data = json.loads(path.read_bytes())
        data["answer"] = "fabricated success"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "pilot_result_binding"):
            module.run_pilot(manifest)

    def test_changed_and_missing_originals_refuse_before_any_call(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        self.material["sources"][0]["mutation"] = "changed"
        self.material_path.write_text(json.dumps(self.material))
        result = module.run_pilot(self.manifest(), initialize=True)
        self.assertEqual(result.model_calls, 0)
        self.assertTrue(all(not t.receipts for t in result.trials))
        self.assertTrue(all(t.outcome != "completed" for t in result.trials))

    def test_invalid_phase_refuses_before_reading_material(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        invalid = manifest.model_copy(update={"phase": "heldout"})
        with (
            mock.patch.object(
                pilot_sources,
                "read_private_file",
                side_effect=AssertionError("read forbidden"),
            ),
            self.assertRaises(ValueError),
        ):
            module.run_pilot(invalid, initialize=True)
        self.assertFalse((self.account / "account.sqlite3").exists())
        self.assertFalse((self.home / "pilot-stores").exists())

    def test_mixed_partition_refused_without_creating_store(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        self.material["cases"][0]["partition"] = "heldout"
        self.material_path.write_text(json.dumps(self.material))
        with self.assertRaisesRegex(ValueError, "pilot_material_binding"):
            module.run_pilot(self.manifest(), initialize=True)
        self.assertFalse((self.home / "pilot-stores").exists())

    def test_no_writer_transaction_at_spawn_and_owner_stops_after_cleanup(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        create = execution.create_wrapper
        stopping = Supervisor.stop
        calls: list[str] = []

        def spawn(spec: Any, *args: Any, **kwargs: Any) -> Any:
            home = store.data_home(os.environ)
            with store.writer_lock(home, wait_s=0):
                pass
            calls.append("spawn")
            return create(spec, *args, **kwargs)

        def stop(owner: Any, now: float) -> None:
            with closing(
                sqlite3.connect(self.account / "account.sqlite3")
            ) as conn:
                records = [
                    json.loads(r[0])
                    for r in conn.execute("SELECT record FROM calls")
                ]
            self.assertTrue(all(r["cleaned"] for r in records))
            calls.append("stop")
            stopping(owner, now)

        with (
            mock.patch.object(execution, "create_wrapper", side_effect=spawn),
            mock.patch.object(Supervisor, "stop", new=stop),
        ):
            result = module.run_pilot(manifest, initialize=True)
        self.assertEqual(result.model_calls, 6)
        self.assertEqual(
            calls, ["spawn"] * 3 + ["stop"] + ["spawn"] * 3 + ["stop"]
        )

    def test_zero_row_ready_persistence_never_activates_child(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        original = pilot_bridge.persist_event
        manifest = self.manifest()
        activations: list[str] = []
        activate = execution.ProcessMonitor.activate

        def persist(home: Any, event: Any, now: float) -> None:
            if not isinstance(event, WorkerReady):
                original(home, event, now)

        def activation(
            monitor: Any, attempt: str, round_index: int, frame: bytes
        ) -> Any:
            activations.append(attempt)
            return activate(monitor, attempt, round_index, frame)

        with (
            mock.patch.object(
                pilot_bridge, "persist_event", side_effect=persist
            ),
            mock.patch.object(
                execution.ProcessMonitor, "activate", new=activation
            ),
        ):
            result = module.run_pilot(manifest, initialize=True)
        self.assertEqual(activations, [])
        self.assertTrue(all(t.outcome != "completed" for t in result.trials))
        self.assertGreater(result.model_calls, 0)

    def test_account_finish_failure_retains_owner_and_blocks_later_arm(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        stops: list[str] = []
        with (
            mock.patch.object(
                Account,
                "finish",
                side_effect=ValueError("injected_account_failure"),
            ),
            mock.patch.object(
                Supervisor,
                "stop",
                side_effect=lambda *args: stops.append("stop"),
            ),
        ):
            result = module.run_pilot(manifest, initialize=True)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(stops, [])
        self.assertEqual(
            result.trials[-1].outcome, "account_recovery_required"
        )
        self.assertTrue(module._RETAINED_OWNERS)

    def test_missing_original_is_a_retained_unavailable_trial(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        self.material["sources"][0]["mutation"] = "missing"
        self.material_path.write_text(json.dumps(self.material))
        result = module.run_pilot(self.manifest(), initialize=True)
        self.assertEqual(len(result.trials), 4)
        self.assertEqual(result.model_calls, 0)
        self.assertFalse(any(t.receipts for t in result.trials))

    def test_task_schema_drift_at_readiness_cancels_activation(self) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        ready = Account.ready
        changed: list[str] = []

        def change(
            account: Any, identity: str, process: Any, now: float
        ) -> None:
            ready(account, identity, process, now)
            call = next(
                c for c in account.calls() if c.schedule.id == identity
            )
            if call.schedule.purpose == "task" and not changed:
                path = manifest.task["new_present"].capability.proof_path
                path.write_bytes(path.read_bytes() + b" ")
                changed.append(identity)

        with mock.patch.object(Account, "ready", new=change):
            result = module.run_pilot(manifest, initialize=True)
        self.assertEqual(len(changed), 1)
        self.assertTrue(
            result.trials[2].receipts,
            "committed learning disappeared when task failed",
        )
        self.assertTrue(all(t.answer is None for t in result.trials))
        with Account(self.account, self.config) as account:
            call = next(
                c for c in account.calls() if c.schedule.id == changed[0]
            )
            self.assertFalse(call.activation_claimed)
            self.assertTrue(call.cleaned)

    def test_judgments_bind_exact_saved_results_and_reject_self_scores(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        first = module.run_pilot(manifest, initialize=True)
        present = first.trials[2]
        value = JudgmentBinding(
            trial_id=present.id,
            result_digest=digest(present.model_dump(mode="json")),
            judgment=IndependentJudgment(
                grader_id=self.config.grader_ids[0],
                method="human",
                grader_version="1",
                evidence_paths=(self.artifacts / (present.id + ".json"),),
                numerators={},
                denominators={},
                later_task_passed=True,
            ),
        )
        again = module.run_pilot(
            manifest.model_copy(update={"judgments": (value,)})
        )
        self.assertEqual(again.model_calls, first.model_calls)
        self.assertEqual(again.report.status, "unverified")
        bad = value.model_copy(update={"result_digest": "0" * 64})
        with self.assertRaisesRegex(ValueError, "pilot_judgment_binding"):
            module.run_pilot(manifest.model_copy(update={"judgments": (bad,)}))

    def test_interrupt_after_learning_preserves_receipt_without_relaunch(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        with (
            mock.patch.object(module, "_task", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            module.run_pilot(manifest, initialize=True)
        with Account(self.account, self.config) as account:
            spent = {c.schedule.id for c in account.calls()}
        spawn = execution.create_wrapper

        def create(spec: Any, monitor: Any, identity: str, *args: Any) -> Any:
            self.assertNotIn(identity, spent)
            return spawn(spec, monitor, identity, *args)

        with mock.patch.object(
            execution, "create_wrapper", side_effect=create
        ):
            second = module.run_pilot(manifest)
        present = next(
            t for t in second.trials if t.comparator == "new_present"
        )
        self.assertTrue(present.receipts)
        self.assertEqual(present.outcome, "unavailable_evidence")
        self.assertIsNone(present.answer)

    def test_crashed_owner_without_checkpoint_blocks_all_later_calls(
        self,
    ) -> None:
        module = importlib.import_module("tools.learning_eval.pilot")
        manifest = self.manifest()
        path = self.root / "manifest.json"
        path.write_text(manifest.model_dump_json())
        program = """import os,sys
from pathlib import Path
from tools.learning_eval import pilot
from tools.learning_eval.pilot_models import PilotManifest
manifest=PilotManifest.model_validate_json(Path(sys.argv[1]).read_bytes())
pilot._one=lambda *args: os._exit(77)
pilot.run_pilot(manifest,initialize=True)
"""
        child = subprocess.run(
            [sys.executable, "-c", program, str(path)],
            check=False,
            timeout=15,
        )
        self.assertEqual(child.returncode, 77)
        started = list(self.artifacts.glob("*-started.json"))
        self.assertEqual(len(started), 1)
        home = Path(json.loads(started[0].read_bytes())["store_home"])
        witness = home / "review.supervisor.json"
        original_witness = witness.read_bytes()
        with (
            pilot_sources.source_environment(home),
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            Supervisor(home, manifest.assessment.policies[0]).start(
                time.time()
            )
        spawn = execution.create_wrapper
        with mock.patch.object(
            execution, "create_wrapper", wraps=spawn
        ) as launched:
            result = module.run_pilot(manifest)
        self.assertEqual(launched.call_count, 0)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(len(result.trials), 4)
        self.assertEqual(result.trials[2].outcome, "quiescence_unverified")
        self.assertEqual(result.trials[3].outcome, "account_recovery_required")
        self.assertEqual(witness.read_bytes(), original_witness)
        with (
            mock.patch.object(
                execution, "create_wrapper", wraps=spawn
            ) as launched,
            self.assertRaisesRegex(ValueError, "quiescence_unverified"),
        ):
            module.run_pilot(manifest)
        self.assertEqual(launched.call_count, 0)
