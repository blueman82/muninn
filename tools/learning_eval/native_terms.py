"""Bind immutable native carry-in evidence to one persistent trial account."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from muninn.review_models import (
    OngoingBudget,
    PilotBudget,
    require_pilot_budget,
)
from muninn.review_runner import check_private_path, read_private_file
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.models import Assessment
from tools.learning_eval.native_predecessor import predecessor
from tools.learning_eval.trial_models import NativeTerms

LIMITATIONS = (
    "cli_launch_accounting_only_internal_model_requests_unknown",
    "cost_unknown_when_unreported_reservations_not_provider_spend_cap",
    "no_universal_no_tools_proof_posthoc_events_may_describe_actions",
    "native_capture_unmeasured_synthetic_sources",
    "production_qualification_unverified",
)

NATIVE_FAILURES = {
    "native_tool_event",
    "deadline",
    "output_limit",
    "invalid_native_output",
    "invalid_worker_protocol",
    "worker_failed",
    "worker_eof",
}


def host_identity() -> str:
    """Bind this host selection without asserting a native capability proof."""
    return digest([platform.system(), platform.machine(), platform.node()])


def authority_bytes(path: Path, expected: str) -> bytes:
    """Read bounded private authority and require the frozen full bytes."""
    raw = read_private_file(path, 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("native_authority_changed")
    strict_json(raw)
    return raw


def validate_budget(
    budget: PilotBudget | OngoingBudget, carried: int = 1
) -> None:
    """Reserve the carried launch outside the remaining account allowance."""
    budget = require_pilot_budget(budget)
    if (
        budget.max_model_calls > 50 - carried
        or budget.max_wall_seconds > 14400
        or budget.currency != "USD"
        or budget.max_cost_decimal is None
        or Decimal(budget.max_cost_decimal) > 250 - 5 * carried
        or budget.conservative_call_cost_decimal is None
        or Decimal(budget.conservative_call_cost_decimal) < 5
    ):
        raise ValueError("native_budget_binding")


def validate_terms(
    terms: NativeTerms, assessment: Assessment, now: float
) -> None:
    """Refuse changed carry-in and enforce the original absolute deadline."""
    if terms.carried_launches == 2:
        predecessor(terms, assessment)
    elif (
        terms.predecessor_path is not None
        or terms.predecessor_digest is not None
    ):
        raise ValueError("native_predecessor_binding")
    if (
        terms.assessment_id != assessment.id
        or terms.account_id != assessment.budget.account_id
        or terms.host_identity != host_identity()
        or Decimal(terms.carried_charge) != 5 * terms.carried_launches
        or terms.receipt_path.parent == terms.account_path
        or terms.account_path in terms.receipt_path.parents
    ):
        raise ValueError("native_terms_binding")
    authority_bytes(terms.owner_boundary_path, terms.owner_boundary_digest)
    _validate_carry_in(terms)
    if not math.isfinite(now) or now < terms.started_at:
        raise ValueError("budget_clock_reversed")
    if now >= assessment_deadline(terms, assessment):
        raise ValueError("budget_wall_limit")
    validate_budget(assessment.budget, terms.carried_launches)
    for policy in assessment.policies:
        validate_budget(policy.budget, terms.carried_launches)
        if policy.max_attempts != 1 or policy.concurrency != 1:
            raise ValueError("native_retry_binding")


def _validate_carry_in(terms: NativeTerms) -> None:
    """Keep the one original canary and its earliest timestamp immutable."""
    parsed_ledger: object = json.loads(
        authority_bytes(terms.carry_in_path, terms.carry_in_digest)
    )
    if not isinstance(parsed_ledger, dict):
        raise ValueError("native_carry_in_binding")
    ledger = cast(dict[str, object], parsed_ledger)
    launches = ledger.get("launches")
    if (
        ledger.get("approved_limits")
        != {
            "max_cli_launches": 50,
            "max_wall_seconds": 14400,
            "max_additional_spend_usd": "250",
            "unknown_cost_reservation_usd_per_launch": "5",
        }
        or ledger.get("scope")
        != {
            "host": "Darwin arm64",
            "provider": "codex_cli",
            "version": "0.163.0",
        }
        or not isinstance(launches, list)
        or len(cast(list[object], launches)) != 1
    ):
        raise ValueError("native_carry_in_binding")
    value = cast(list[object], launches)[0]
    if not isinstance(value, dict):
        raise ValueError("native_carry_in_binding")
    first = cast(dict[str, object], value)
    if (
        first.get("ordinal") != 1
        or first.get("reserved_usd") != "5"
        or first.get("state") != "completed"
        or first.get("actual_cost_usd") is not None
        or first.get("actual_model_requests") is not None
        or first.get("exit_code") != 0
        or first.get("reserved_at_utc") != ledger.get("start_utc")
    ):
        raise ValueError("native_carry_in_binding")
    start = ledger.get("start_utc")
    if not isinstance(start, str):
        raise ValueError("native_carry_in_binding")
    parsed = datetime.fromisoformat(start)
    if parsed.tzinfo is None or parsed.timestamp() != terms.started_at:
        raise ValueError("native_carry_in_binding")


def _receipt(path: Path, raw: bytes) -> None:
    """Create a durable private import marker or require its exact bytes."""
    check_private_path(path.parent, directory=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if read_private_file(path, 1024 * 1024) != raw:
            raise ValueError("native_account_reset") from None
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def bind_account(account: Account, terms: NativeTerms, now: float) -> None:
    """Import once under Account ownership, refusing replacement or reset."""
    validate_terms(terms, account.assessment, now)
    if account.path != terms.account_path:
        raise ValueError("native_account_binding")
    stat = (account.path / "account.sqlite3").stat()
    raw = json.dumps(
        {
            "terms": terms.model_dump(mode="json"),
            "account_device": stat.st_dev,
            "account_inode": stat.st_ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    hashed = hashlib.sha256(raw).hexdigest()
    prior = account.result_binding("native-carry-in")
    exists = terms.receipt_path.exists()
    if (exists and prior != hashed) or (not exists and prior is not None):
        raise ValueError("native_account_reset")
    if prior is None and account.calls():
        raise ValueError("native_account_reset")
    _receipt(terms.receipt_path, raw)
    account.result_binding("native-carry-in", hashed)
    _receipt(
        account.path / "native-carry-in.json",
        authority_bytes(terms.carry_in_path, terms.carry_in_digest),
    )
    stopped = stop_reason(account)
    if stopped is not None:
        raise ValueError(stopped)
    _, charged = account_totals(account, terms)
    if Decimal(charged) > 250:
        raise ValueError("budget_cost_limit")


def account_totals(account: Account, terms: NativeTerms) -> tuple[int, str]:
    """Report combined spent launches and reservations without cost claims."""
    total = Decimal(terms.carried_charge)
    for call in account.calls():
        reserved = Decimal(call.cost_reservation_decimal or "5")
        known = call.usage.cost_decimal if call.usage is not None else None
        total += max(reserved, Decimal(known or "0"))
    return terms.carried_launches + len(account.calls()), str(total)


def assessment_deadline(terms: NativeTerms, assessment: Assessment) -> float:
    """Apply every wall ceiling to the original canary start."""
    return terms.started_at + min(
        14400,
        assessment.budget.max_wall_seconds,
        *(
            require_pilot_budget(p.budget).max_wall_seconds
            for p in assessment.policies
        ),
    )


def stop_reason(account: Account) -> str | None:
    """Persist native timeout and observed-action stopping across restarts."""
    codes = {c.outcome for c in account.calls()}
    if "native_tool_event" in codes:
        return "native_trial_stopped_after_tool_event"
    if "deadline" in codes:
        return "native_trial_stopped_after_timeout"
    if codes & NATIVE_FAILURES:
        return "native_trial_stopped_after_uncertain_output"
    return None
