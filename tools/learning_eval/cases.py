"""Load strict labelled cases and preserve family partition boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from muninn.knowledge_models import Provider
from muninn.review_json import finite_float, reject_constant, unique_object
from tools.learning_eval.models import (
    Assessment,
    Case,
    CaseFile,
    HostRequirement,
    Trial,
)


def strict_json(raw: bytes) -> None:
    """Reject duplicate keys and nonfinite extensions before model parsing."""
    json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def digest(value: object) -> str:
    """Hash a canonical JSON-compatible value with no inferred defaults."""
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def load_cases(path: Path) -> tuple[Case, ...]:
    """Read sanitized synthetic cases and retain uncertainty in their file."""
    raw = path.read_bytes()
    strict_json(raw)
    file = CaseFile.model_validate_json(raw)
    validate_partition(file.cases)
    ids = {case.id for case in file.cases}
    families = {case.family_id for case in file.cases}
    if (
        not set(file.uncertain_case_ids) <= ids
        or families != file.families.keys()
    ):
        raise ValueError("case_metadata_mismatch")
    uncertain = {
        c.id
        for c in file.cases
        if not c.expected.eligible_claim_keys
        and not c.expected.must_not_save_claim_keys
    }
    if set(file.uncertain_case_ids) != uncertain or len(
        file.uncertain_case_ids
    ) != len(uncertain):
        raise ValueError("uncertainty_mismatch")
    return file.cases


def validate_partition(cases: tuple[Case, ...]) -> None:
    """Reject cross-partition families, relabelled copies and reversed time."""
    ids: set[str] = set()
    families: dict[str, tuple[str, int]] = {}
    roots: dict[tuple[str, str], str] = {}
    lines: dict[tuple[str, str], tuple[int, float]] = {}
    for case in cases:
        previous = families.get(case.family_id)
        if case.id in ids or (
            previous is not None
            and (
                previous[0] != case.partition or previous[1] >= case.chronology
            )
        ):
            raise ValueError("partition_leakage")
        ids.add(case.id)
        families[case.family_id] = (case.partition, case.chronology)
        for record in case.evidence:
            root = (record.provider, record.session_root)
            if root in roots and roots[root] != case.family_id:
                raise ValueError("partition_leakage")
            roots[root] = case.family_id
            key = (case.family_id, record.thread_id)
            position = (record.line, record.selected_at)
            if key in lines and (
                position[0] < lines[key][0] or position[1] < lines[key][1]
            ):
                raise ValueError("partition_leakage")
            lines[key] = position


def input_digests(cases: tuple[Case, ...]) -> dict[str, str]:
    """Bind original evidence, independent labels and reserved families."""
    return {
        "cases_digest": digest([c.model_dump(mode="json") for c in cases]),
        "labels_digest": digest(
            [
                {"id": c.id, "expected": c.expected.model_dump(mode="json")}
                for c in cases
            ]
        ),
        "heldout_family_digest": digest(
            sorted({c.family_id for c in cases if c.partition == "heldout"})
        ),
    }


def config_digest(assessment: Assessment) -> str:
    """Hash every declared setting except its own digest field."""
    return digest(
        assessment.model_dump(mode="json", exclude={"config_digest"})
    )


def required_host_seed() -> tuple[HostRequirement, ...]:
    """Return the twelve required combinations with no version claims."""
    providers: tuple[Provider, ...] = ("claude", "codex", "cursor")
    platforms = (
        ("macos", "arm64", "launchd"),
        ("linux", "x64", "systemd"),
        ("windows", "x64", "scheduled_task"),
        ("windows", "arm64", "scheduled_task"),
    )
    return tuple(
        HostRequirement(
            id=f"{os_name}-{arch}-{provider}",
            os=os_name,
            architecture=arch,
            host_surface="local",
            provider=provider,
            service=service,
            required_events=(
                "session_start",
                "completion",
                "resume",
                "compaction",
            ),
        )
        for os_name, arch, service in platforms
        for provider in providers
    )


def phase_cases(
    cases: tuple[Case, ...], phase: Literal["pilot", "heldout"]
) -> tuple[Case, ...]:
    """Select a declared partition without moving cases between families."""
    return tuple(case for case in cases if case.partition == phase)


def validate_trial_bindings(
    trials: tuple[Trial, ...], assessment: Assessment
) -> None:
    """Bind policy and requested comparator settings before scoring."""
    policies = {digest(p.model_dump(mode="json")) for p in assessment.policies}
    for trial in trials:
        if trial.policy_digest not in policies:
            raise ValueError("unfrozen_policy")
        requested = digest(trial.requested.model_dump(mode="json"))
        if (
            assessment.comparator_config_digests.get(trial.comparator)
            != requested
        ):
            raise ValueError("unfrozen_configuration")
