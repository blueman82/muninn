"""Define explicit synthetic pilot inputs, frozen schedules and evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from muninn.knowledge_models import Digest, SchemaVersion, StrictModel, Text
from muninn.review_evidence_models import (
    EvidenceWindow,
    NonnegativeInt,
    PositiveInt,
)
from muninn.review_models import CostDecimal, Receipt
from muninn.review_runner import RunnerCapability
from tools.learning_eval.cases import digest
from tools.learning_eval.models import (
    Assessment,
    Case,
    Comparator,
    EvaluationReport,
    IndependentJudgment,
    TrialAuthorization,
)
from tools.learning_eval.task_protocol import protocol_schema
from tools.learning_eval.trial_models import NativeTerms, ScheduledCall


class RunnerBinding(StrictModel):
    """Select exact candidate bytes and their existing synthetic authority."""

    capability: RunnerCapability
    authorization: TrialAuthorization


class ExposureRecipe(StrictModel):
    """Freeze deterministic context, lexical search and guide-open bounds."""

    context_bytes: PositiveInt
    search_limit: NonnegativeInt
    guide_open_limit: NonnegativeInt
    guide_bytes: NonnegativeInt


class SourceMaterial(StrictModel):
    """Declare sanitized originals independently of grading labels."""

    case_id: Text
    provider: Literal["codex"]
    thread_id: Text
    session_root: Text
    relative_path: Path
    content: Text
    sha256: Digest
    window: EvidenceWindow
    mutation: Literal["none", "missing", "changed"]


class PilotMaterial(StrictModel):
    """Contain only pilot cases, never a path to reserved heldout material."""

    schema_version: SchemaVersion
    synthetic: Literal[True]
    phase: Literal["pilot"]
    families: dict[Text, Text]
    cases: tuple[Case, ...]
    sources: tuple[SourceMaterial, ...]


class JudgmentBinding(StrictModel):
    """Attach external attribution only to exact saved trial evidence."""

    trial_id: Text
    result_digest: Digest
    judgment: IndependentJudgment


class PilotManifest(StrictModel):
    """Bind declared synthetic dependencies before opening pilot material."""

    schema_version: SchemaVersion
    origin: Literal["synthetic", "native_codex_trial"]
    native_terms: NativeTerms | None = None
    phase: Literal["pilot"]
    assessment: Assessment
    material_path: Path
    material_digest: Digest
    family_digest: Digest
    reserved_heldout_digest: Digest
    learner_prompt_path: Path
    task_instructions: Text
    learning: dict[Comparator, RunnerBinding]
    task: dict[Comparator, RunnerBinding]
    account_path: Path
    artifact_path: Path
    exposures: dict[Text, ExposureRecipe]
    judgments: tuple[JudgmentBinding, ...]


class PlannedTrial(StrictModel):
    """Fix population identity separately from potential model-call rounds."""

    id: Text
    case_id: Text
    family_id: Text
    comparator: Comparator
    repetition: PositiveInt
    pair_id: Digest
    store_home: Path
    recipe: ExposureRecipe
    calls: tuple[ScheduledCall, ...]

    def result(self, code: str) -> PilotTrial:
        """Retain frozen population identity for any terminal outcome."""
        return PilotTrial(
            **self.model_dump(exclude={"calls", "recipe"}), outcome=code
        )


class Exposure(StrictModel):
    """Distinguish observed retrieval channels from bytes sent to the task."""

    note_ids: tuple[Text, ...]
    guide_ids: tuple[Text, ...]
    fts_ids: tuple[Text, ...]
    opened_ids: tuple[Text, ...]
    omitted_ids: tuple[Text, ...]
    context_truncated: bool
    query: str
    prompt_context: dict[str, object]
    learned_digest: Digest


class PilotTrial(StrictModel):
    """Retain every scheduled row and its actual nullable synthetic result."""

    id: Text
    case_id: Text
    family_id: Text
    comparator: Comparator
    repetition: PositiveInt
    pair_id: Digest
    store_home: Path
    outcome: Text
    receipts: tuple[Receipt, ...] = ()
    exposure: Exposure | None = None
    answer: str | None = None
    output_paths: tuple[Path, ...] = ()
    call_ids: tuple[Text, ...] = ()
    unnecessary_call_ids: tuple[Text, ...] = ()


class PilotResult(StrictModel):
    """Report scripted synthetic outcomes without native or measured claims."""

    origin: Literal["synthetic", "native_codex_trial"] = "synthetic"
    trials: tuple[PilotTrial, ...]
    predecessor_trials: tuple[PilotTrial, ...] = ()
    predecessor_trial_map: dict[Text, Text] = Field(default_factory=dict)
    predecessor_receipt_digest: Digest | None = None
    model_calls: NonnegativeInt | None
    cli_launches: NonnegativeInt | None = None
    reserved_cost_decimal: CostDecimal | None = None
    report: EvaluationReport


def planned_calls(
    manifest: PilotManifest, ident: str, comparator: Comparator
) -> tuple[ScheduledCall, ...]:
    """Enumerate bounded learner rounds and the separate task call slot."""
    assessment, policy = manifest.assessment, manifest.assessment.policies[0]
    calls: list[ScheduledCall] = []
    for purpose, bindings in (
        ("learning", manifest.learning),
        ("task", manifest.task),
    ):
        protocol = (
            "muninn-review-round-v1"
            if purpose == "learning"
            else "muninn-task-answer-v1"
        )
        retries = policy.max_attempts if purpose == "learning" else 1
        rounds = policy.max_rounds if purpose == "learning" else 1
        for retry in range(1, retries + 1):
            calls.extend(
                ScheduledCall.model_validate_json(
                    json.dumps(
                        {
                            "id": "A_"
                            + digest([ident, purpose, retry, index])[:32],
                            "assessment_id": assessment.id,
                            "comparator": comparator,
                            "trial_id": ident,
                            "retry_index": retry,
                            "round_index": index,
                            "purpose": purpose,
                            "protocol": protocol,
                            "schema_digest": digest(protocol_schema(protocol)),
                            "policy_digest": digest(
                                policy.model_dump(mode="json")
                            ),
                            "config_digest": assessment.config_digest,
                            "authorization_digest": digest(
                                bindings[comparator].authorization.model_dump(
                                    mode="json"
                                )
                            ),
                        }
                    )
                )
                for index in range(1, rounds + 1)
            )
    return tuple(calls)


def schedule(
    manifest: PilotManifest, material: PilotMaterial
) -> tuple[PlannedTrial, ...]:
    """Freeze complete samples independently of conditional call envelopes."""
    assessment = manifest.assessment
    trials: list[PlannedTrial] = []
    cases = sorted(
        material.cases,
        key=lambda c: (digest([assessment.seed, c.family_id]), c.chronology),
    )
    for case in cases:
        for repetition in range(1, assessment.repetitions + 1):
            recipe = manifest.exposures[case.id]
            pair = digest(
                [
                    case.family_id,
                    case.id,
                    repetition,
                    recipe.model_dump(mode="json"),
                ]
            )
            for comparator in assessment.comparators:
                ident = "T_" + digest([assessment.id, pair, comparator])
                home = (
                    manifest.learning[comparator].authorization.disposable_home
                    / "pilot-stores"
                    / digest([case.family_id, comparator, repetition])
                )
                trials.append(
                    PlannedTrial(
                        id=ident,
                        case_id=case.id,
                        family_id=case.family_id,
                        comparator=comparator,
                        repetition=repetition,
                        pair_id=pair,
                        store_home=home,
                        recipe=recipe,
                        calls=planned_calls(manifest, ident, comparator),
                    )
                )
    return tuple(trials)
