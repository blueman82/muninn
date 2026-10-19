"""Validate frozen synthetic inputs and score without launching processes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from tools.learning_eval.cases import (
    config_digest,
    input_digests,
    load_cases,
    phase_cases,
    required_host_seed,
    strict_json,
    validate_trial_bindings,
)
from tools.learning_eval.models import OfflineManifest
from tools.learning_eval.report import write_report
from tools.learning_eval.score import score_trials


def validate_manifest(
    raw: bytes, phase: Literal["pilot", "heldout"]
) -> OfflineManifest:
    """Refuse missing approval, freeze or mismatched evidence."""
    strict_json(raw)
    manifest = OfflineManifest.model_validate_json(raw)
    assessment = manifest.assessment
    if assessment.phase != phase:
        raise ValueError("phase_mismatch")
    if assessment.required_hosts != required_host_seed():
        raise ValueError("required_matrix_mismatch")
    cases = load_cases(manifest.cases_path)
    actual = input_digests(cases)
    for key, value in actual.items():
        if getattr(assessment, key) != value:
            raise ValueError("input_digest_mismatch")
    if config_digest(assessment) != assessment.config_digest:
        raise ValueError("config_digest_mismatch")
    if len(phase_cases(cases, phase)) != assessment.sample_size:
        raise ValueError("sample_size_mismatch")
    validate_trial_bindings(manifest.trials, assessment)
    return manifest


def main() -> int:
    """Score only explicit synthetic manifests with no live execution route."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "heldout"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    phase: Literal["pilot", "heldout"] = args.phase
    try:
        manifest = validate_manifest(args.manifest.read_bytes(), phase)
        cases = phase_cases(load_cases(manifest.cases_path), phase)
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
