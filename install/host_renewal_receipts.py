"""Publish strict host-renewal receipts and recognize retained grant facts."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from install import legacy_baseline as baseline
from install import policy_records, record
from install.context import Ctx, StepFailedError
from install.host_renewal_models import HostState, PendingFacts, RenewalReceipt
from install.record import Record


def phase(
    ctx: Ctx, rec: Record, name: str, **fields: object
) -> RenewalReceipt:
    """Validate the complete prospective receipt before durable publication."""
    value = dict(rec["host_renewal"]) | fields | {"phase": name}
    checked = policy_records.host_renewal(value)
    prospective = rec | {"host_renewal": checked.model_dump(mode="json")}
    policy_records.check_renewal_size(prospective)
    record.save(ctx, prospective)
    rec.update(prospective)
    return checked


def verified(ctx: Ctx, rec: Record) -> None:
    """Retain the actual normal verification report without another probe."""
    raw = json.loads(baseline.read_bytes(ctx.rdir / "verify.json"))
    outcomes: dict[str, bool | None] = {}
    for item in raw["checks"]:
        key, value = item["check"], item["ok"]
        if key in outcomes:
            prior = outcomes[key]
            value = (
                False
                if False in (prior, value)
                else None if None in (prior, value) else True
            )
        outcomes[key] = value
    rec.pop("recovery_required", None)
    rec.pop("failed", None)
    phase(
        ctx,
        rec,
        "upgrade_verified",
        verification=[
            {"check": key, "ok": value}
            for key, value in sorted(outcomes.items())
        ],
    )


def recognize(
    conn: sqlite3.Connection, receipt: RenewalReceipt, current: HostState
) -> Literal["predecessor", "successor"]:
    """Recognize only exact pending state without replay, fencing or writes."""
    if (
        conn.in_transaction
        or not isinstance(receipt, PendingFacts)
        or receipt.phase
        not in {
            "commit_pending",
            "grant_committed",
            "restart_pending",
            "upgrade_verified",
        }
    ):
        raise StepFailedError("host_renewal_recovery_required")
    retained = receipt.contained.model_copy(
        update={"control": current.control}
    )
    if current != retained:
        raise StepFailedError("host_renewal_state_changed")
    if current.control == receipt.predecessor:
        return "predecessor"
    if current.control == receipt.successor:
        return "successor"
    raise StepFailedError("host_renewal_state_changed")


def check_future_records(
    rec: Record, pending: RenewalReceipt, steps: tuple[str, ...]
) -> None:
    """Check complete larger normal-success envelopes before the grant."""
    for name in (
        "grant_committed",
        "restart_pending",
        "upgrade_verified",
    ):
        fields = (
            {
                "verification": [
                    {"check": key, "ok": False}
                    for key in (
                        "codex_hook_hashes_match",
                        "codex_resolves_new_hooks",
                        "doctor",
                        "heartbeat_fresh",
                        "hook_runs",
                        "managed_service_verified",
                        "new_pid_alive",
                        "runtime_bootstrap",
                    )
                ]
            }
            if name == "upgrade_verified"
            else {}
        )
        policy_records.check_renewal_size(
            rec
            | {
                "steps": list(steps),
                "install_complete": True,
                "sweep_failed": [],
                "host_renewal": pending.model_dump(mode="json")
                | {"phase": name}
                | fields,
            }
        )
