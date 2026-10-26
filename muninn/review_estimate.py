"""Estimate queue time from comparable completed calls."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from statistics import median

from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy, Usage


def estimate(
    conn: sqlite3.Connection, policy: ReviewPolicy, requests: int
) -> dict[str, str | int | float | None]:
    """Keep unsupported cost and allowance unknown."""
    rows = conn.execute(
        "SELECT ended_at-coalesce(launched_at,reserved_at) AS "
        "seconds,usage_json "
        "FROM review_call WHERE state='completed' AND ended_at IS NOT NULL "
        "AND requested_json=? AND ended_at>coalesce(launched_at,reserved_at) "
        "ORDER BY ended_at DESC LIMIT 50",
        (canonical_json(policy.runner),),
    ).fetchall()
    durations = [float(row["seconds"]) for row in rows]
    value: dict[str, str | int | float | None] = {
        "sample_calls": len(rows),
        "confidence": "low" if len(rows) < 20 else "estimate",
        "seconds_low": None,
        "seconds_high": None,
        "api_cost_decimal": None,
        "currency": None,
        "subscription_allowance": None,
        "cost_note": "The remaining subscription allowance is unknown.",
    }
    if durations:
        seconds = requests * median(durations) / policy.concurrency
        value.update(
            seconds_low=round(seconds, 1),
            seconds_high=round(seconds * policy.max_rounds, 1),
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
            cost_note=(
                "Estimate from reported costs. "
                "Subscription cost is unknown."
            ),
        )
    return value
