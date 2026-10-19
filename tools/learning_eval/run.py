"""Validate frozen synthetic inputs and score without launching processes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from tools.learning_eval.capability import (
    prepare_capability_trial as prepare_capability_trial,
)
from tools.learning_eval.cases import (
    config_digest,
    input_digests,
    load_cases,
    phase_cases,
    required_host_seed,
    strict_json,
    validate_trial_bindings,
)
from tools.learning_eval.models import Assessment, Case, OfflineManifest
from tools.learning_eval.pilot import run_pilot as run_pilot
from tools.learning_eval.report import write_report
from tools.learning_eval.score import score_trials


def validate_manifest(
    raw: bytes,
    phase: Literal["pilot", "heldout"],
    *,
    frozen_assessment: Assessment | None = None,
) -> OfflineManifest:
    """Refuse missing approval, freeze or mismatched evidence."""
    manifest, _ = _validated_manifest(raw, phase, frozen_assessment)
    return manifest


def _validated_manifest(
    raw: bytes,
    phase: Literal["pilot", "heldout"],
    frozen_assessment: Assessment | None,
) -> tuple[OfflineManifest, tuple[Case, ...]]:
    """Return the exact validated material so scoring never rereads it."""
    strict_json(raw)
    manifest = OfflineManifest.model_validate_json(raw)
    assessment = manifest.assessment
    if assessment.phase != phase:
        raise ValueError("phase_mismatch")
    if assessment.required_hosts != required_host_seed():
        raise ValueError("required_matrix_mismatch")
    if config_digest(assessment) != assessment.config_digest:
        raise ValueError("config_digest_mismatch")
    if phase == "heldout":
        if frozen_assessment is None:
            raise ValueError("freeze_binding_required")
        frozen = Assessment.model_validate_json(
            frozen_assessment.model_dump_json()
        )
        if (
            frozen != assessment
            or config_digest(frozen) != frozen.config_digest
        ):
            raise ValueError("freeze_binding_mismatch")
    cases = load_cases(manifest.cases_path)
    actual = input_digests(cases)
    for key, value in actual.items():
        if getattr(assessment, key) != value:
            raise ValueError("input_digest_mismatch")
    if len(phase_cases(cases, phase)) != assessment.sample_size:
        raise ValueError("sample_size_mismatch")
    validate_trial_bindings(manifest.trials, assessment)
    return manifest, cases


def main() -> int:
    """Score only explicit synthetic manifests with no live execution route."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "heldout"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    phase: Literal["pilot", "heldout"] = args.phase
    try:
        manifest, material = _validated_manifest(
            args.manifest.read_bytes(), phase, None
        )
        cases = phase_cases(material, phase)
        report = score_trials(
            cases,
            manifest.trials,
            manifest.assessment,
            host_results=manifest.host_results,
        )
        report = report.model_copy(
            update={
                "status": (
                    "revise" if report.status == "revise" else "unverified"
                ),
                "limitations": (
                    *report.limitations,
                    "synthetic_not_measured_performance",
                ),
            }
        )
        if report.release_blockers:
            report = report.model_copy(update={"status": "revise"})
        write_report(report, args.output / "report.json")
        (args.output / "trials.json").write_text(
            json.dumps(
                {
                    "synthetic": True,
                    "trials": [
                        t.model_dump(mode="json") for t in manifest.trials
                    ],
                },
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except (ValueError, OSError):
        print(json.dumps({"error": "assessment_prerequisite_refused"}))
        return 2
    print(
        json.dumps(
            {"status": report.status, "synthetic": True, "runner_calls": 0}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
