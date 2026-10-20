"""Keep later-task answers distinct from reviewer proposals and slots."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import unittest
from contextlib import closing

from muninn.review_models import ObservedConfig
from tests.test_learning_eval_account import LearningAccountTests
from tests.test_learning_eval_contracts import FIXTURE, judgment, trial
from tools.learning_eval.cases import digest, load_cases
from tools.learning_eval.score import paired_values


class TaskProtocolTests(unittest.TestCase):
    """Reject malformed task output before any interpretation."""

    def test_answer_only_protocol(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.learning_eval.task_protocol"),
            "fixed task answer protocol is absent",
        )
        module = importlib.import_module("tools.learning_eval.task_protocol")
        self.assertEqual(
            module.parse_answer(b'{"answer":"renew"}', 100).answer, "renew"
        )
        for raw in (
            b'{"op":"final","result":{}}',
            b'{"answer":"x","operation":"save"}',
            b'{"answer":"x","answer":"y"}',
            b'{"answer":NaN}',
            b'{"answer":1}',
            b"invalid",
            b'{"answer":"' + b"x" * 100 + b'"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                module.parse_answer(raw, 100)
        prompt = module.task_prompt(
            "trusted", "restore access", {"notes": []}, 1000
        )
        self.assertEqual(json.loads(prompt)["query"], "restore access")


class PurposeSlotTests(LearningAccountTests):
    """Preserve legacy learning slots while distinguishing declared tasks."""

    def test_learning_and_task_round_one_are_distinct(self) -> None:
        self.assertIn(
            "purpose",
            self.models.ScheduledCall.model_fields,
            "learning and task share the same logical slot",
        )
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            learn = self.schedule()
            call, _ = account.claim(learn, self.auth, 10.0)
            account.finish(call.schedule.id, None, "synthetic", 10.0)
            task = self.schedule(
                2,
                trial_id=learn.trial_id,
                purpose="task",
                protocol="muninn-task-answer-v1",
                schema_digest=digest(
                    importlib.import_module(
                        "tools.learning_eval.task_protocol"
                    ).task_schema()
                ),
            )
            account.plan(task, self.auth)
            self.assertTrue(account.claim(task, self.auth, 11.0)[1])
            with self.assertRaisesRegex(ValueError, "call_binding_mismatch"):
                account.claim(
                    task.model_copy(update={"id": "A_" + "f" * 32}),
                    self.auth,
                    11.0,
                )

    def test_historical_spent_learning_reopens_without_permit(self) -> None:
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            call, _ = account.claim(self.schedule(), self.auth, 10.0)
            account.finish(call.schedule.id, None, "synthetic", 10.0)
        with (
            closing(sqlite3.connect(self.path / "account.sqlite3")) as conn,
            conn,
        ):
            row = conn.execute("SELECT record FROM calls").fetchone()
            data = json.loads(row[0])
            for key in ("purpose", "protocol", "schema_digest"):
                data["schedule"].pop(key, None)
            conn.execute("UPDATE calls SET record=?", (json.dumps(data),))
            conn.execute(
                "UPDATE scheduled SET record=?",
                (json.dumps(data["schedule"]),),
            )
        with self.module.Account(self.path, self.config) as account:
            self.assertFalse(
                account.claim(self.schedule(), self.auth, 11.0)[1]
            )
            self.assertEqual(len(account.calls()), 1)


class PairIdentityTests(unittest.TestCase):
    """Pair exact sample keys independently of report row ordering."""

    def test_reordered_missing_duplicate_and_historical_pairs(self) -> None:
        case = load_cases(FIXTURE)[0]
        config = ObservedConfig(
            runner="fake", version="1", model="saving", reasoning="high"
        )
        passed = judgment(0, 0).model_copy(
            update={
                "numerators": {},
                "denominators": {},
                "later_task_passed": True,
            }
        )
        failed = passed.model_copy(update={"later_task_passed": False})
        common = {
            "requested": config,
            "observed": config,
            "learned_state_digest": "c" * 64,
        }
        left = trial(
            case.id, id="p1", pair_id="a" * 64, judgment=passed, **common
        )
        right = trial(
            case.id,
            id="w1",
            comparator="new_withheld",
            pair_id="a" * 64,
            judgment=failed,
            **common,
        )
        left2 = left.model_copy(
            update={"id": "p2", "pair_id": "b" * 64, "judgment": failed}
        )
        right2 = right.model_copy(
            update={"id": "w2", "pair_id": "b" * 64, "judgment": passed}
        )
        expected = paired_values(
            (case,), (left, right, left2, right2), "new_present"
        )
        self.assertEqual(
            expected,
            paired_values(
                (case,), (right2, left, right, left2), "new_present"
            ),
        )
        self.assertFalse(paired_values((case,), (left,), "new_present")[1])
        self.assertFalse(
            paired_values((case,), (left, right, left), "new_present")[1]
        )
        historical = tuple(
            t.model_copy(update={"pair_id": None}) for t in (left, right)
        )
        self.assertEqual(
            paired_values((case,), historical, "new_present"), ({}, False)
        )


class DeclaredTaskTests(LearningAccountTests):
    """Require task stages to be declared before requesting their permit."""

    def test_task_requires_a_predeclared_stage_and_exact_schema(self) -> None:
        module = importlib.import_module("tools.learning_eval.task_protocol")
        with self.module.Account(
            self.path, self.config, initialize=True
        ) as account:
            task = self.schedule(
                purpose="task",
                protocol="muninn-task-answer-v1",
                schema_digest=digest(module.task_schema()),
            )
            with self.assertRaisesRegex(ValueError, "schedule_not_declared"):
                account.claim(task, self.auth, 10.0)
            account.plan(task, self.auth)
            self.assertTrue(account.claim(task, self.auth, 10.0)[1])
