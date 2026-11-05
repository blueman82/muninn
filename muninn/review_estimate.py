"""Estimate queue time from comparable completed calls."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from math import ceil
from statistics import median

from muninn.review_models import ReviewPolicy, Usage
from muninn.review_runtime_models import (
    ManagedCodexSelection,
    RunnerConfig,
    RuntimeIdentity,
)


def estimate(
    conn: sqlite3.Connection,
    policy: ReviewPolicy,
    requests: int,
    *,
    execution: RuntimeIdentity | None = None,
) -> dict[str, str | int | float | None]:
    """Keep unsupported cost and allowance unknown."""
    predicate, arguments = _runtime_predicate(policy, execution)
    rows = conn.execute(
        "SELECT ended_at-coalesce(launched_at,reserved_at) AS seconds,usage_j"
        "son "
        "FROM review_call WHERE state='completed' AND ended_at IS NOT NULL "
        f"AND ({predicate}) "
        "AND coalesce(outcome,'')!='not_launched' "
        "AND ended_at>coalesce(launched_at,reserved_at) "
        "ORDER BY ended_at DESC LIMIT 50",
        arguments,
    ).fetchall()
    durations = [float(row["seconds"]) for row in rows]
    value: dict[str, str | int | float | None] = {
        "sample_calls": len(rows),
        "confidence": "low" if len(rows) < 20 else "estimate",
        "seconds_low": None,
        "seconds_high": None,
        "api_cost_decimal": None,
        "retry_api_cost_decimal": None,
        "retry_seconds_high": None,
        "time_note": "The first range excludes retries. The retry "
        "estimate includes all permitted attempts and backoffs.",
        "currency": None,
        "subscription_allowance": None,
        "cost_note": "The remaining subscription allowance is unknown.",
    }
    if isinstance(policy.runner, ManagedCodexSelection):
        value["runtime_status"] = (
            "checked" if execution is not None else "unknown"
        )
    if durations:
        batches = ceil(requests / policy.concurrency)
        seconds = batches * median(durations)
        value.update(
            seconds_low=round(seconds, 1),
            seconds_high=round(seconds * policy.max_rounds, 1),
            retry_seconds_high=round(
                batches
                * (
                    median(durations) * policy.max_rounds * policy.max_attempts
                    + sum(policy.retry_backoff_seconds)
                ),
                1,
            ),
        )
    usages = [
        Usage.model_validate_json(row["usage_json"])
        for row in rows
        if row["usage_json"] is not None
    ]
    if (
        len(usages) == len(rows)
        and usages
        and all(
            u.cost_decimal is not None and u.currency == usages[0].currency
            for u in usages
        )
    ):
        cost = sum(
            (Decimal(u.cost_decimal or "0") for u in usages), Decimal(0)
        ) / len(usages)
        value.update(
            api_cost_decimal=str(cost * requests * policy.max_rounds),
            currency=usages[0].currency,
            retry_api_cost_decimal=str(
                cost * requests * policy.max_rounds * policy.max_attempts
            ),
            cost_note=(
                "Estimate from reported costs. "
                "Subscription cost is unknown."
            ),
        )
    return value


def _runtime_predicate(
    policy: ReviewPolicy, execution: RuntimeIdentity | None
) -> tuple[str, tuple[str, ...]]:
    """Compare explicit launched facts or retain an unknown managed runtime."""
    requested = policy.runner
    keys = ("runner", "executable", "version", "model", "reasoning")
    if isinstance(requested, RunnerConfig):
        return " AND ".join(
            f"json_extract(requested_json,'$.{key}')=?" for key in keys
        ), tuple(getattr(requested, key) for key in keys)
    if execution is None or (
        execution.requested.model,
        execution.requested.reasoning,
    ) != (requested.model, requested.reasoning):
        return "0", ()
    return (
        "json_extract(requested_json,'$.record')='codex-managed-call-v1' "
        "AND json_extract(requested_json,'$.selection.manager_root')=? "
        "AND json_extract(requested_json,'$.selection.model')=? "
        "AND json_extract(requested_json,'$.selection.reasoning')=? "
        "AND json_extract(requested_json,'$.launched_identity')=? "
        "AND EXISTS (SELECT 1 FROM json_each(requested_json,'$.candidates') c"
        "andidate "
        "WHERE json_extract(candidate.value,'$.capability_key')=? "
        "AND json_extract(candidate.value,'$.requested.executable')=? "
        "AND json_extract(candidate.value,'$.requested.version')=?)",
        (
            str(requested.manager_root),
            requested.model,
            requested.reasoning,
            execution.digest,
            execution.capability_key,
            execution.requested.executable,
            execution.requested.version,
        ),
    )
