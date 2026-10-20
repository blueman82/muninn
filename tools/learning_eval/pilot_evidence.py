"""Keep immutable bounded private evidence outside content-free accounts."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from muninn import store
from muninn.knowledge_models import Digest, StrictModel
from muninn.review_budget import call_view
from muninn.review_json import canonical_json
from muninn.review_models import ObservedConfig, Usage
from muninn.review_process import ProcessOutcome
from muninn.review_runner import check_private_path, read_private_file
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.pilot_models import (
    PilotManifest,
    PilotTrial,
    PlannedTrial,
)
from tools.learning_eval.trial_models import AccountCall, ScheduledCall


class SavedOutput(StrictModel):
    """Bind decoded content to an exact immutable spent call and input."""

    schedule: ScheduledCall
    input_digest: Digest
    output: str
    exit_code: int | None
    error_code: str | None
    elapsed_seconds: float
    observed: ObservedConfig
    usage: Usage | None
    learner_mapping_digest: Digest | None


@dataclass(frozen=True)
class ExecutionResult:
    """Return one charged call and available contained output evidence."""

    call: AccountCall
    outcome: ProcessOutcome | None
    evidence_path: Path | None


def immutable(root: Path, name: str, raw: bytes, limit: int) -> Path:
    """Create private exact bytes or require an identical artifact."""
    check_private_path(root, directory=True)
    if Path(name).name != name or len(raw) > limit:
        raise ValueError("artifact_invalid")
    path = root / name
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        check_private_path(path, directory=False)
        if read_private_file(path, limit) != raw:
            raise ValueError("artifact_conflict") from None
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return path


def saved_output(
    root: Path,
    account: Account,
    call: AccountCall,
    prompt: bytes,
    outcome: ProcessOutcome,
    mapping: str | None,
    limit: int,
) -> ExecutionResult:
    """Publish only after the account has durably retained the spent call."""
    value = SavedOutput(
        schedule=call.schedule,
        input_digest=hashlib.sha256(prompt).hexdigest(),
        output=outcome.stdout.decode("utf-8"),
        exit_code=outcome.exit_code,
        error_code=outcome.error_code,
        elapsed_seconds=outcome.elapsed_seconds,
        observed=outcome.observed,
        usage=outcome.usage,
        learner_mapping_digest=mapping,
    )
    raw = canonical_json(value).encode()
    path = immutable(root, call.schedule.id + ".json", raw, limit)
    account.bind_evidence(call.schedule.id, hashlib.sha256(raw).hexdigest())
    bound = next(
        c for c in account.calls() if c.schedule.id == call.schedule.id
    )
    return ExecutionResult(bound, outcome, path)


def restore_output(
    root: Path,
    call: AccountCall,
    prompt: bytes,
    mapping: str | None,
    limit: int,
) -> ExecutionResult:
    """Never turn missing or corrupt evidence into a second launch permit."""
    if call.evidence_digest is None:
        return ExecutionResult(call, None, None)
    path = root / (call.schedule.id + ".json")
    try:
        check_private_path(root, directory=True)
        check_private_path(path, directory=False)
        raw = read_private_file(path, limit)
        strict_json(raw)
        value = SavedOutput.model_validate_json(raw)
        if (
            hashlib.sha256(raw).hexdigest() != call.evidence_digest
            or value.schedule != call.schedule
            or value.input_digest != hashlib.sha256(prompt).hexdigest()
            or value.learner_mapping_digest != mapping
            or not call.cleaned
            or value.usage != call.usage
            or value.error_code != call.outcome
        ):
            raise ValueError("artifact_binding")
    except (ValueError, OSError):
        return ExecutionResult(call, None, None)
    outcome = ProcessOutcome(
        call.schedule.id,
        call.schedule.round_index,
        value.output.encode(),
        value.exit_code,
        value.error_code,
        value.elapsed_seconds,
        value.observed,
        value.usage,
    )
    return ExecutionResult(call, outcome, path)


def reopen_trial(
    manifest: PilotManifest, plan: PlannedTrial, account: Account
) -> PilotTrial | None:
    """Verify charged outputs and receipts before reusing results."""
    path = manifest.artifact_path / (plan.id + ".json")
    partial = not path.exists()
    if partial:
        path = manifest.artifact_path / (plan.id + "-learning.json")
    if not path.exists():
        return None
    check_private_path(path, directory=False)
    raw = read_private_file(path, 4 * 1024 * 1024)
    bound = account.result_binding(path.stem)
    if bound is None:
        return plan.result("unavailable_evidence")
    if hashlib.sha256(raw).hexdigest() != bound:
        raise ValueError("pilot_result_binding")
    strict_json(raw)
    result = PilotTrial.model_validate_json(raw)
    if result.model_dump(
        include={
            "id",
            "case_id",
            "family_id",
            "comparator",
            "repetition",
            "pair_id",
            "store_home",
        }
    ) != plan.model_dump(exclude={"recipe", "calls"}):
        raise ValueError("pilot_result_binding")
    _verify_receipts(plan.store_home, result)
    available = _verify_outputs(manifest.artifact_path, result, account)
    if partial or not available:
        return result.model_copy(
            update={
                "outcome": "unavailable_evidence",
                "answer": None,
                "output_paths": () if not available else result.output_paths,
            }
        )
    return result


def _verify_outputs(root: Path, result: PilotTrial, account: Account) -> bool:
    """Require every referenced output to remain bound to the same charge."""
    calls = {c.schedule.id: c for c in account.calls()}
    for output in result.output_paths:
        call = calls.get(output.stem)
        if (
            output.parent != root
            or call is None
            or call.schedule.trial_id != result.id
        ):
            raise ValueError("pilot_result_binding")
        try:
            check_private_path(output, directory=False)
            content = read_private_file(output, 4 * 1024 * 1024)
        except (ValueError, OSError):
            return False
        if hashlib.sha256(content).hexdigest() != call.evidence_digest:
            return False
        saved = SavedOutput.model_validate_json(content)
        if saved.schedule != call.schedule or saved.usage != call.usage:
            raise ValueError("pilot_output_binding")
        if saved.learner_mapping_digest is not None:
            _verify_mapping(root, result.store_home, saved)
    return True


def _verify_receipts(home: Path, result: PilotTrial) -> None:
    """Match exact durable receipt identities and all published fields."""
    if result.receipts:
        with closing(store.connect_ro(store.db_path(home))) as conn:
            for receipt in result.receipts:
                row = conn.execute(
                    "SELECT * FROM review_receipt WHERE request_id=?",
                    (receipt.request_id,),
                ).fetchone()
                if (
                    row is None
                    or row["attempt_id"] != receipt.attempt_id
                    or json.loads(row["applied_json"])
                    != [v.model_dump(mode="json") for v in receipt.applied]
                    or json.loads(row["unresolved_json"])
                    != receipt.unresolved_ids
                    or row["committed_at"] != receipt.committed_at
                    or row["outcome"] != receipt.outcome
                    or row["no_change_reason"] != receipt.no_change_reason
                ):
                    raise ValueError("pilot_receipt_binding")


def save_trial(
    manifest: PilotManifest,
    result: PilotTrial,
    account: Account,
    suffix: str = "",
) -> None:
    """Bind an immutable stage checkpoint before moving to another stage."""
    raw = canonical_json(result).encode()
    identity = result.id + suffix
    immutable(manifest.artifact_path, identity + ".json", raw, 4 * 1024 * 1024)
    account.result_binding(identity, hashlib.sha256(raw).hexdigest())


def _verify_mapping(root: Path, home: Path, saved: SavedOutput) -> None:
    """Verify historical learner identity and its completed ledger row."""
    path = root / (saved.schedule.id + "-mapping.json")
    check_private_path(path, directory=False)
    raw = read_private_file(path, 65536)
    strict_json(raw)
    mapping: dict[str, object] = json.loads(raw)
    if digest(mapping) != saved.learner_mapping_digest:
        raise ValueError("pilot_mapping_binding")
    if mapping.get("home") != str(home) or mapping.get(
        "schedule"
    ) != saved.schedule.model_dump(mode="json"):
        raise ValueError("pilot_mapping_binding")
    with closing(store.connect_ro(store.db_path(home))) as conn:
        row = conn.execute(
            "SELECT c.* FROM review_call c JOIN review_attempt a "
            "ON a.id=c.attempt_id WHERE c.attempt_id=? AND c.round_index=? "
            "AND a.request_id=? AND a.generation=?",
            (
                mapping.get("attempt_id"),
                saved.schedule.round_index,
                mapping.get("request_id"),
                mapping.get("generation"),
            ),
        ).fetchone()
    if row is None:
        raise ValueError("pilot_mapping_binding")
    call = call_view(row)
    if (
        call.usage != saved.usage
        or call.observed != saved.observed
        or call.outcome != (saved.error_code or "completed")
        or call.state
        != ("completed" if saved.error_code is None else "unknown")
    ):
        raise ValueError("pilot_mapping_binding")
