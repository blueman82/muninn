"""Verify one stopped native predecessor from immutable read-only evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from muninn.review_runner import read_private_file
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.models import Assessment
from tools.learning_eval.pilot_evidence import SavedOutput
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotResult,
    PlannedTrial,
)
from tools.learning_eval.trial_models import (
    AccountCall,
    NativeTerms,
    ScheduledCall,
)


def _object(value: object) -> dict[str, object]:
    """Require an object without accepting arbitrary JSON shapes."""
    if not isinstance(value, dict):
        raise ValueError("native_predecessor_binding")
    return cast(dict[str, object], value)


def _read(path: Path, expected: str, *, binary: bool = False) -> bytes:
    """Read the exact frozen bytes without reopening the prior Account."""
    raw = read_private_file(path, 4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("native_predecessor_changed")
    if not binary:
        strict_json(raw)
    return raw


def _evidence(ruling: dict[str, object], name: str) -> tuple[Path, bytes]:
    """Resolve one explicitly hashed predecessor artifact."""
    record = _object(_object(ruling.get("evidence")).get(name))
    path, hashed = record.get("path"), record.get("sha256")
    if not isinstance(path, str) or not isinstance(hashed, str):
        raise ValueError("native_predecessor_binding")
    location = Path(path)
    raw = _read(location, hashed, binary=name == "account")
    if record.get("bytes") != len(raw):
        raise ValueError("native_predecessor_binding")
    return location, raw


def predecessor(
    terms: NativeTerms, assessment: Assessment
) -> tuple[PilotManifest, PilotResult, tuple[PlannedTrial, ...]]:
    """Prove exactly one cleaned failed claim plus the original canary."""
    if terms.predecessor_path is None or terms.predecessor_digest is None:
        raise ValueError("native_predecessor_required")
    ruling = _object(
        json.loads(_read(terms.predecessor_path, terms.predecessor_digest))
    )
    old = PilotManifest.model_validate_json(_evidence(ruling, "manifest")[1])
    result = PilotResult.model_validate_json(_evidence(ruling, "result")[1])
    raw_plans: object = json.loads(
        _evidence(ruling, "predeclared_schedule")[1]
    )
    if not isinstance(raw_plans, list):
        raise ValueError("native_predecessor_binding")
    plans = tuple(
        PlannedTrial.model_validate_json(json.dumps(p))
        for p in cast(list[object], raw_plans)
    )
    prior = old.native_terms
    if (
        prior is None
        or prior.carried_launches != 1
        or prior.predecessor_path is not None
        or prior.predecessor_digest is not None
        or old.origin != "native_codex_trial"
        or result.origin != old.origin
        or prior.started_at != terms.started_at
        or prior.host_identity != terms.host_identity
        or prior.carry_in_digest != terms.carry_in_digest
        or prior.carry_in_path != terms.carry_in_path
        or prior.owner_boundary_digest != terms.owner_boundary_digest
        or prior.owner_boundary_path != terms.owner_boundary_path
        or terms.carried_launches != 2
        or Decimal(terms.carried_charge) != 10
        or prior.assessment_id == assessment.id
        or prior.account_id == terms.account_id
        or prior.account_path == terms.account_path
        or prior.receipt_path == terms.receipt_path
        or result.cli_launches != 2
        or result.reserved_cost_decimal != "10"
        or result.model_calls is not None
        or result.predecessor_trials
        or not plans
        or len(plans) > 6
        or len(plans) != len(result.trials)
        or len({p.id for p in plans}) != len(plans)
    ):
        raise ValueError("native_predecessor_binding")
    canary_path, canary_raw = _evidence(ruling, "original_canary")
    if (
        canary_path != terms.carry_in_path
        or hashlib.sha256(canary_raw).hexdigest() != terms.carry_in_digest
    ):
        raise ValueError("native_predecessor_binding")
    _successor(ruling, old.assessment, assessment, terms, len(plans))
    _account(ruling, old, result, plans)
    return old, result, plans


def _successor(
    ruling: dict[str, object],
    prior: Assessment,
    current: Assessment,
    terms: NativeTerms,
    population: int,
) -> None:
    """Allow only the approved identity and tighter remaining allowance."""
    successor, facts = _object(ruling.get("successor")), _object(
        ruling.get("facts")
    )
    start = facts.get("started_at_utc")
    directory = successor.get("subdirectory")
    if (
        not isinstance(start, str)
        or datetime.fromisoformat(start).tzinfo is None
        or datetime.fromisoformat(start).timestamp() != terms.started_at
        or not isinstance(directory, str)
        or Path(directory) not in terms.account_path.parents
        or successor.get("assessment_id") != current.id
        or successor.get("account_id") != current.budget.account_id
        or successor.get("carried_launches") != 2
        or successor.get("carried_reservations_usd") != "10"
        or successor.get("max_additional_launches") != 48
        or successor.get("max_remaining_reservations_usd") != "240"
        or successor.get("new_trials") != population
        or successor.get("same_population") is not True
        or facts.get("total_spent_cli_launches") != 2
        or facts.get("retained_reservations_usd") != "10"
        or facts.get("actual_model_requests") is not None
        or facts.get("actual_cost_usd") is not None
        or prior.model_dump(
            exclude={"id", "budget", "policies", "config_digest"}
        )
        != current.model_dump(
            exclude={"id", "budget", "policies", "config_digest"}
        )
        or len(prior.policies) != 1
        or len(current.policies) != 1
        or prior.policies[0].model_dump(exclude={"budget"})
        != current.policies[0].model_dump(exclude={"budget"})
    ):
        raise ValueError("native_predecessor_binding")


def _account(
    ruling: dict[str, object],
    manifest: PilotManifest,
    result: PilotResult,
    plans: tuple[PlannedTrial, ...],
) -> None:
    """Inspect a verified SQLite byte snapshot without writing prior files."""
    terms = manifest.native_terms
    assert terms is not None
    path, raw = _evidence(ruling, "account")
    receipt_path, receipt_raw = _evidence(ruling, "import_receipt")
    receipt = _object(json.loads(receipt_raw))
    stat = path.stat()
    if (
        path != manifest.account_path / "account.sqlite3"
        or receipt_path != terms.receipt_path
        or NativeTerms.model_validate_json(json.dumps(receipt.get("terms")))
        != terms
        or receipt.get("account_device") != stat.st_dev
        or receipt.get("account_inode") != stat.st_ino
    ):
        raise ValueError("native_predecessor_binding")
    assessment = manifest.assessment
    identity = digest(
        {
            "id": assessment.id,
            "budget": assessment.budget.model_dump(mode="json"),
            "approved_by": assessment.approved_by,
            "approved_at": assessment.approved_at,
        }
    )
    try:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.deserialize(raw)
            calls = tuple(
                AccountCall.model_validate_json(row[0])
                for row in conn.execute("SELECT record FROM calls")
            )
            scheduled = tuple(
                ScheduledCall.model_validate_json(row[0])
                for row in conn.execute(
                    "SELECT record FROM scheduled ORDER BY rowid"
                )
            )
            bindings = dict(conn.execute("SELECT trial,digest FROM results"))
            if conn.execute("SELECT identity FROM metadata").fetchall() != [
                (identity,)
            ]:
                raise ValueError("native_predecessor_binding")
    except sqlite3.DatabaseError:
        raise ValueError("native_predecessor_binding") from None
    declared = tuple(c for p in plans for c in p.calls)
    if (
        len(calls) != 1
        or scheduled != declared
        or bindings.get("native-carry-in")
        != hashlib.sha256(receipt_raw).hexdigest()
    ):
        raise ValueError("native_predecessor_binding")
    call = calls[0]
    failed_path = _failed_output(ruling, call, declared, terms.started_at)
    fields = {
        "id",
        "case_id",
        "family_id",
        "comparator",
        "repetition",
        "pair_id",
        "store_home",
    }
    for plan, trial in zip(plans, result.trials, strict=True):
        failed_trial = trial.id == call.schedule.trial_id
        if (
            trial.model_dump(include=fields) != plan.model_dump(include=fields)
            or bindings.get(trial.id) != digest(trial.model_dump(mode="json"))
            or trial.outcome
            != (
                "worker_failed"
                if failed_trial
                else "native_trial_stopped_after_uncertain_output"
            )
            or trial.call_ids != ((call.schedule.id,) if failed_trial else ())
            or trial.output_paths != ((failed_path,) if failed_trial else ())
        ):
            raise ValueError("native_predecessor_binding")


def _failed_output(
    ruling: dict[str, object],
    call: AccountCall,
    declared: tuple[ScheduledCall, ...],
    started_at: float,
) -> Path:
    """Retain the failed CLI's unknown usage and exact contained output."""
    failed_path, failed_raw = _evidence(ruling, "failed_output")
    failed = SavedOutput.model_validate_json(failed_raw)
    if (
        not call.cleaned
        or not call.activation_claimed
        or call.process_identity is None
        or call.state != "unknown"
        or call.outcome != "worker_failed"
        or call.usage is not None
        or call.cost_reservation_decimal != "5"
        or call.currency != "USD"
        or call.schedule not in declared
        or failed.schedule != call.schedule
        or failed.error_code != "worker_failed"
        or failed.exit_code != 1
        or failed.output != ""
        or failed.usage is not None
        or call.evidence_digest != hashlib.sha256(failed_raw).hexdigest()
        or call.reserved_at < started_at
    ):
        raise ValueError("native_predecessor_binding")
    return failed_path


def continuation_history(
    manifest: PilotManifest, plans: tuple[PlannedTrial, ...]
) -> tuple[PilotResult | None, dict[str, str]]:
    """Link the same logical rows without adding them to successor scoring."""
    terms = manifest.native_terms
    if terms is None or terms.carried_launches == 1:
        return None, {}
    old, result, prior_plans = predecessor(terms, manifest.assessment)
    if (
        old.model_dump(
            include={
                "material_path",
                "material_digest",
                "family_digest",
                "reserved_heldout_digest",
                "task_instructions",
                "exposures",
            }
        )
        != manifest.model_dump(
            include={
                "material_path",
                "material_digest",
                "family_digest",
                "reserved_heldout_digest",
                "task_instructions",
                "exposures",
            }
        )
        or len(plans) != len(prior_plans)
        or sum(len(p.calls) for p in plans) > 18
        or manifest.artifact_path == old.artifact_path
    ):
        raise ValueError("native_predecessor_population")
    mapping: dict[str, str] = {}
    fields = {
        "case_id",
        "family_id",
        "comparator",
        "repetition",
        "pair_id",
        "recipe",
    }
    for before, after in zip(prior_plans, plans, strict=True):
        if (
            before.model_dump(include=fields)
            != after.model_dump(include=fields)
            or before.id == after.id
            or before.store_home == after.store_home
            or [
                (c.purpose, c.round_index, c.retry_index, c.schema_digest)
                for c in before.calls
            ]
            != [
                (c.purpose, c.round_index, c.retry_index, c.schema_digest)
                for c in after.calls
            ]
        ):
            raise ValueError("native_predecessor_population")
        mapping[before.id] = after.id
    return result, mapping
