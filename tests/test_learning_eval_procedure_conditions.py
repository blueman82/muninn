"""Check conditional procedural evidence without claiming model quality."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path

from muninn import knowledge_read, review_evidence, store
from muninn.knowledge_models import GuideBody, Scope
from tests.test_learning_eval_pilot import PilotFixture
from tools.learning_eval.cases import config_digest, input_digests
from tools.learning_eval.models import Case
from tools.learning_eval.pilot import run_pilot
from tools.learning_eval.pilot_models import (
    ExposureRecipe,
    PilotManifest,
    PilotTrial,
)
from tools.learning_eval.pilot_retrieval import observe
from tools.learning_eval.pilot_sources import load_material, source_environment

FIXTURE = Path(__file__).parent / "fixtures/learning/procedure-conditions.json"
CONDITION = "The credential is expired and the renewal endpoint is reachable."
PROCEDURE = "Renew the expired credential, then retry sign-in."
CASE_IDS = (
    "condition_success",
    "condition_paraphrase",
    "condition_absent",
    "failure_unknown",
    "recalled_repeat",
)
PROGRAM = """import json,sys
from pathlib import Path
p=json.load(sys.stdin)
with Path('../procedure-prompts.jsonl').open('a') as log:
    log.write(json.dumps(p)+'\\n')
if p['protocol']=='muninn-task-answer-v1':
    op={'answer':'scripted exposure observation'}
else:
    d=p['data']
    guides=d['knowledge_context']['guides']
    if d['round_index']==1:
        if guides:
            op={'op':'open','knowledge_id':guides[0]['id'],
                'revision':guides[0]['revision']}
        else:
            op={'op':'search','query':'renewal','scope':d['scope']}
    else:
        changes=[]
        if not guides:
            refs=[d['initial']['excerpts'][0]['id']]
            claim={'text':'Renew the expired credential, then retry sign-in.',
                   'evidence_refs':refs}
            condition={'text':'The credential is expired and the renewal '
                              'endpoint is reachable.', 'evidence_refs':refs}
            changes=[{'action':'add','form':'guide','kind':'procedure',
                      'scope':d['scope'],'title':'Conditional renewal',
                      'summary':claim,'applicability':[condition],
                      'steps':[{'key':'renew','claim':claim,'depends_on':[]}],
                      'decision_points':[],'pitfalls':[],
                      'reason':'Observed conditional success'}]
        op={'op':'final','result':{'schema_version':1,'changes':changes,
            'unresolved':[],'no_change_reason':None if changes else
            'No independent procedural observation'}}
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'structured_output':op}))
"""


class ProcedureConditionsTests(PilotFixture):
    """Exercise checked storage and retrieval with an isolated fake child."""

    def prepare(self) -> PilotManifest:
        """Bind physical sources, labels and runner bytes before scheduling."""
        self.assertTrue(FIXTURE.is_file(), "procedural family is absent")
        self.material = json.loads(FIXTURE.read_bytes())
        self.assertEqual(
            set(self.material["families"]),
            {"conditioned-renewal"},
            "procedural family is absent",
        )
        self.assertEqual(
            tuple(c["id"] for c in self.material["cases"]), CASE_IDS
        )
        self.material_path.write_text(json.dumps(self.material))
        self.executable.write_text("#!" + sys.executable + "\n" + PROGRAM)
        cases = tuple(
            Case.model_validate_json(json.dumps(c))
            for c in self.material["cases"]
        )
        self.assertEqual({c.partition for c in cases}, {"pilot"})
        self.assertEqual([c.chronology for c in cases], [1, 2, 3, 4, 5])
        bindings = input_digests(cases)
        bindings.pop("heldout_family_digest")
        budget = self.config.budget.model_copy(
            update={"max_model_calls": 30, "max_cost_decimal": "30"}
        )
        policy = self.config.policies[0].model_copy(update={"budget": budget})
        self.config = self.config.model_copy(
            update={
                **bindings,
                "budget": budget,
                "policies": (policy,),
                "sample_size": 5,
            }
        )
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        manifest = self.manifest()
        recipe = ExposureRecipe(
            context_bytes=12000,
            search_limit=5,
            guide_open_limit=2,
            guide_bytes=12000,
        )
        return manifest.model_copy(
            update={"exposures": dict.fromkeys(CASE_IDS, recipe)}
        )

    def guide(self, trial: PilotTrial) -> GuideBody:
        """Read the real current guide and require exactly one revision."""
        with closing(
            store.connect_ro(store.db_path(trial.store_home))
        ) as conn:
            values = list(
                knowledge_read.current_knowledge(
                    conn, Scope(project_id="/work/repo", task=None), 2e9
                )
            )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].revision, 1)
        body = values[0].body
        self.assertIsInstance(body, GuideBody)
        assert isinstance(body, GuideBody)
        self.assertEqual(body.summary.text, PROCEDURE)
        self.assertEqual([c.text for c in body.applicability], [CONDITION])
        return body

    def test_five_cases_keep_condition_and_original_support(self) -> None:
        manifest = self.prepare()
        result = run_pilot(manifest, initialize=True)
        present = {
            t.case_id: t
            for t in result.trials
            if t.comparator == "new_present"
        }
        withheld = {
            t.case_id: t
            for t in result.trials
            if t.comparator == "new_withheld"
        }
        self.assertEqual(set(present), set(CASE_IDS))
        self.assertEqual(set(withheld), set(CASE_IDS))
        self.assertEqual(result.model_calls, 30)
        self.assertEqual(result.report.status, "unverified")
        for case_id in CASE_IDS:
            with self.subTest(case_id=case_id):
                trial, omitted = present[case_id], withheld[case_id]
                self.assertEqual(trial.outcome, "completed")
                self.assertEqual(omitted.outcome, "completed")
                self.assertEqual(trial.pair_id, omitted.pair_id)
                self.assertNotEqual(trial.store_home, omitted.store_home)
                assert trial.exposure and omitted.exposure
                self.assertTrue(trial.exposure.opened_ids)
                self.assertEqual(omitted.exposure.prompt_context, {})
                self.assertEqual(
                    trial.exposure.learned_digest,
                    omitted.exposure.learned_digest,
                )
                self.assertEqual(
                    len(trial.receipts[0].applied),
                    1 if case_id == "condition_success" else 0,
                )
        with self.subTest(case_id="condition_success", check="support"):
            self.check_success(present["condition_success"])
        with self.subTest(case_id="condition_paraphrase", check="retrieval"):
            self.check_paraphrase(present["condition_paraphrase"])
        with self.subTest(case_id="condition_absent", check="applicability"):
            self.check_absent(present["condition_absent"])
        with self.subTest(case_id="failure_unknown", check="pitfalls"):
            self.check_unknown(present["failure_unknown"])
        with self.subTest(case_id="recalled_repeat", check="support"):
            self.check_repeat(present["recalled_repeat"], manifest)
        self.check_labels_and_frozen_retrieval(present, manifest)

    def check_success(self, trial: PilotTrial) -> None:
        """Catch lost applicability or support from outside the observation."""
        body = self.guide(trial)
        with closing(
            store.connect_ro(store.db_path(trial.store_home))
        ) as conn:
            rows = conn.execute(
                "SELECT DISTINCT e.id,e.line FROM knowledge_support s "
                "JOIN review_evidence e ON e.id=s.evidence_id"
            ).fetchall()
        self.assertEqual([r[1] for r in rows], [2])
        self.assertEqual(body.summary.evidence_refs, [rows[0][0]])
        self.assertEqual(body.applicability[0].evidence_refs, [rows[0][0]])
        assert trial.exposure
        opened = trial.exposure.prompt_context["opened"]
        self.assertIn(CONDITION, json.dumps(opened))
        self.assertIn(PROCEDURE, json.dumps(opened))

    def check_paraphrase(self, trial: PilotTrial) -> None:
        """Catch missing guide exposure when lexical retrieval finds no hit."""
        self.guide(trial)
        assert trial.exposure
        self.assertFalse(trial.exposure.fts_ids)
        self.assertTrue(trial.exposure.guide_ids)
        self.assertTrue(trial.exposure.opened_ids)
        self.assertIn(CONDITION, json.dumps(trial.exposure.prompt_context))

    def check_absent(self, trial: PilotTrial) -> None:
        """Keep advisory exposure conditional when the nearby task lacks A."""
        self.guide(trial)
        case = next(
            c for c in self.material["cases"] if c["id"] == trial.case_id
        )
        self.assertEqual(case["expected"]["retrieval_claim_keys"], [])
        self.assertIn(
            "LABEL_UNCONDITIONAL_GENERALIZATION",
            case["expected"]["must_not_save_claim_keys"],
        )
        assert trial.exposure
        self.assertIn(CONDITION, json.dumps(trial.exposure.prompt_context))
        self.assertFalse(trial.receipts[0].applied)

    def check_unknown(self, trial: PilotTrial) -> None:
        """Catch a saved proved pitfall in this scripted uncertain episode."""
        body = self.guide(trial)
        self.assertEqual(body.pitfalls, [])
        self.assertFalse(trial.receipts[0].applied)
        with closing(
            store.connect_ro(store.db_path(trial.store_home))
        ) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM knowledge_claim "
                    "WHERE claim_key LIKE 'pitfall%'"
                ).fetchone()[0],
                0,
            )
            row = conn.execute(
                "SELECT evidence_type,authority,excerpt FROM review_evidence "
                "WHERE line=5"
            ).fetchone()
        self.assertEqual(tuple(row[:2]), ("assistant_report", "reported"))
        self.assertIn("Cause unknown", row[2])

    def check_repeat(self, trial: PilotTrial, manifest: PilotManifest) -> None:
        """Keep original guide support and observe repeat provenance."""
        body = self.guide(trial)
        home = trial.store_home
        source = load_material(manifest).sources[-1]
        with source_environment(home):
            projected = review_evidence.initial_evidence(
                home, source.window, 12000
            ).excerpts
        self.assertEqual(
            [(e.line, e.evidence_type, e.authority) for e in projected],
            [
                (6, "owner_statement", "untrusted"),
                (7, "assistant_report", "reported"),
            ],
        )
        with closing(store.connect_ro(store.db_path(home))) as conn:
            supports = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT evidence_id FROM knowledge_support"
                )
            }
            repeated = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM review_evidence WHERE line IN (6,7)"
                )
            }
            current = next(
                knowledge_read.current_knowledge(
                    conn, Scope(project_id="/work/repo", task=None), 2e9
                )
            )
            resolved = knowledge_read.knowledge_support(
                conn, current.id, current.revision, body.scope, 2e9
            )
        self.assertEqual(supports, set(body.summary.evidence_refs))
        self.assertEqual(set(resolved), supports)
        self.assertEqual(len(repeated), 2)
        self.assertTrue(supports.isdisjoint(repeated))
        self.assertFalse(trial.receipts[0].applied)
        prompts = (self.home / "procedure-prompts.jsonl").read_text()
        repeat_rounds = [
            p["data"]
            for raw in prompts.splitlines()
            if (p := json.loads(raw))["protocol"] == "muninn-review-prompt-v1"
            and p["data"]["round_index"] == 2
            and p["data"]["initial"]["excerpts"][0]["line"] == 6
        ]
        self.assertEqual(len(repeat_rounds), 2)
        opened = next(
            d
            for d in repeat_rounds
            if set(d["history"][0][1]["body"]["summary"]["evidence_refs"])
            == supports
        )
        self.assertEqual(set(opened["evidence_ids"]), supports | repeated)
        self.assertEqual(opened["history"][0][0]["op"], "open")

    def check_labels_and_frozen_retrieval(
        self, trials: dict[str, PilotTrial], manifest: PilotManifest
    ) -> None:
        """Keep labels private and compare retrieval on frozen state."""
        prompts = (self.home / "procedure-prompts.jsonl").read_text()
        labels = [c["expected"] for c in self.material["cases"]]
        for expected in labels:
            for key in (
                "eligible_claim_keys",
                "must_not_save_claim_keys",
                "retrieval_claim_keys",
            ):
                for marker in expected[key]:
                    self.assertNotIn(marker, prompts)
            self.assertNotIn(expected["later_task_success_rule"], prompts)
            self.assertNotIn(expected["labeler_id"], prompts)
            for path in expected["label_evidence_paths"]:
                self.assertNotIn(path, prompts)
        trial = trials["recalled_repeat"]
        scope = Scope(project_id="/work/repo", task=None)
        recipe = manifest.exposures[trial.case_id]
        with closing(
            store.connect_ro(store.db_path(trial.store_home))
        ) as conn:
            before = conn.execute("SELECT * FROM knowledge_support").fetchall()
        with source_environment(trial.store_home):
            visible = observe(
                trial.store_home,
                scope,
                "renewal",
                recipe,
                self.config.policies[0],
                withheld=False,
            )
            hidden = observe(
                trial.store_home,
                scope,
                "renewal",
                recipe,
                self.config.policies[0],
                withheld=True,
            )
        assert trial.exposure
        self.assertEqual(visible.learned_digest, trial.exposure.learned_digest)
        self.assertEqual(visible.learned_digest, hidden.learned_digest)
        self.assertEqual(visible.opened_ids, hidden.opened_ids)
        self.assertTrue(visible.prompt_context["opened"])
        self.assertEqual(hidden.prompt_context, {})
        with closing(
            store.connect_ro(store.db_path(trial.store_home))
        ) as conn:
            after = conn.execute("SELECT * FROM knowledge_support").fetchall()
        self.assertEqual(before, after)

    def test_missing_family_is_rejected_before_store_creation(self) -> None:
        manifest = self.prepare()
        self.material["families"] = {}
        self.material_path.write_text(json.dumps(self.material))
        manifest = manifest.model_copy(
            update={
                "material_digest": hashlib.sha256(
                    self.material_path.read_bytes()
                ).hexdigest()
            }
        )
        with self.assertRaisesRegex(ValueError, "pilot_material_binding"):
            load_material(manifest)
        self.assertFalse((self.home / "pilot-stores").exists())
