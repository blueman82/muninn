"""Separate lexical matches from pushed notes and eligible guide bodies."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from muninn.knowledge_models import Scope
from tests.test_learning_eval_pilot import PilotFixture
from tools.learning_eval.cases import config_digest, input_digests
from tools.learning_eval.models import Case
from tools.learning_eval.pilot import run_pilot
from tools.learning_eval.pilot_models import ExposureRecipe
from tools.learning_eval.pilot_retrieval import observe
from tools.learning_eval.pilot_sources import source_environment

FIXTURE = (
    Path(__file__).parent / "fixtures/learning/procedural-paraphrase.json"
)
PROGRAM = """import json,sys
p=json.load(sys.stdin)
if p['protocol']=='muninn-task-answer-v1':
    known='renew expired credential' in json.dumps(p['knowledge'])
    op={'answer':'renew expired credential' if known else 'unknown'}
else:
    d=p['data']
    if d['round_index']==1:
        op={'op':'search','query':'credential','scope':d['scope']}
    else:
        claim={'text':'renew expired credential',
               'evidence_refs':[d['evidence_ids'][0]]}
        note={'action':'add','form':'note','kind':'procedure',
              'scope':d['scope'],'content':claim['text'],'conditions':[],
              'evidence_refs':claim['evidence_refs'],'reason':'Owner lesson'}
        changes=[note]
        if 'Guide:' in json.dumps(d['initial']):
            guide={'action':'add','form':'guide','kind':'procedure',
                   'scope':d['scope'],'title':'Credential renewal',
                   'summary':claim,'applicability':[],
                   'steps':[{'key':'renew','claim':claim,'depends_on':[]}],
                   'decision_points':[],'pitfalls':[],'reason':'Owner guide'}
            irrelevant=dict(note,content='green status badges')
            changes=[guide,irrelevant]
        op={'op':'final','result':{'schema_version':1,'changes':changes,
            'unresolved':[],'no_change_reason':None}}
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'structured_output':op}))
"""


class ParaphrasePilotTests(PilotFixture):
    """Use original-backed stored knowledge, never labels for retrieval."""

    def test_literal_paraphrase_guide_omission_and_wrong_scope(self) -> None:
        self.material = json.loads(FIXTURE.read_bytes())
        self.material_path.write_text(json.dumps(self.material))
        self.executable.write_text("#!" + sys.executable + "\n" + PROGRAM)
        budget = self.config.budget.model_copy(
            update={"max_model_calls": 24, "max_cost_decimal": "24"}
        )
        policy = self.config.policies[0].model_copy(update={"budget": budget})
        cases = tuple(
            Case.model_validate_json(json.dumps(c))
            for c in self.material["cases"]
        )
        hashes = input_digests(cases)
        hashes.pop("heldout_family_digest")
        self.config = self.config.model_copy(
            update={
                **hashes,
                "budget": budget,
                "policies": (policy,),
                "sample_size": 3,
            }
        )
        self.config = self.config.model_copy(
            update={"config_digest": config_digest(self.config)}
        )
        manifest = self.manifest()
        recipe = manifest.exposures["literal"]
        manifest = manifest.model_copy(
            update={
                "exposures": {
                    "literal": recipe,
                    "paraphrase": recipe,
                    "omitted": recipe.model_copy(
                        update={"context_bytes": 100, "guide_open_limit": 0}
                    ),
                }
            }
        )
        result = run_pilot(manifest, initialize=True)
        present = {
            t.case_id: t
            for t in result.trials
            if t.comparator == "new_present"
        }
        self.assertTrue(
            all(t.outcome == "completed" for t in present.values()),
            [t.outcome for t in present.values()],
        )
        literal, paraphrase, omitted = (
            present[key] for key in ("literal", "paraphrase", "omitted")
        )
        assert literal.exposure and paraphrase.exposure and omitted.exposure
        self.assertTrue(literal.exposure.fts_ids)
        self.assertFalse(paraphrase.exposure.fts_ids)
        self.assertTrue(paraphrase.exposure.note_ids)
        self.assertTrue(paraphrase.exposure.guide_ids)
        self.assertTrue(paraphrase.exposure.opened_ids)
        self.assertEqual(paraphrase.answer, "renew expired credential")
        self.assertTrue(omitted.exposure.context_truncated)
        self.assertFalse(omitted.exposure.note_ids)
        self.assertFalse(omitted.exposure.opened_ids)
        self.assertTrue(omitted.exposure.fts_ids)
        self.assertEqual(omitted.answer, "unknown")
        with source_environment(literal.store_home):
            wrong = observe(
                literal.store_home,
                Scope(project_id="/other/project", task=None),
                "credential",
                ExposureRecipe(
                    context_bytes=10000,
                    search_limit=5,
                    guide_open_limit=2,
                    guide_bytes=10000,
                ),
                policy,
                withheld=False,
            )
        self.assertEqual(
            (wrong.note_ids, wrong.guide_ids, wrong.fts_ids, wrong.opened_ids),
            ((), (), (), ()),
        )
        self.assertEqual(result.model_calls, 18)
