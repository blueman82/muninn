"""Share immutable charge arithmetic across independent call ledgers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, localcontext

from muninn.review_models import CallState, OngoingBudget, PilotBudget, Usage


@dataclass(frozen=True)
class Charge:
    """Project cost and lifecycle without runner-specific admission data."""

    state: CallState
    reserved_at: float
    usage: Usage | None
    cost_reservation_decimal: str | None
    currency: str | None


def limits(
    calls: list[Charge],
    budget: PilotBudget | OngoingBudget,
    charge: str | None,
    now: float,
) -> None:
    """Check call counts and elapsed time from the first reservation."""
    if calls and now < max(call.reserved_at for call in calls):
        raise ValueError("budget_clock_reversed")
    if isinstance(budget, OngoingBudget):
        return
    if len(calls) >= budget.max_model_calls:
        raise ValueError("budget_call_limit")
    if (
        calls
        and now - min(call.reserved_at for call in calls)
        >= budget.max_wall_seconds
    ):
        raise ValueError("budget_wall_limit")
    if budget.max_cost_decimal is not None:
        if charge is None:
            raise ValueError("budget_charge_unavailable")
        charges = [retained_charge(call, budget.currency) for call in calls]
        if Decimal(sum_decimal([*charges, charge])) > Decimal(
            budget.max_cost_decimal
        ):
            raise ValueError("budget_cost_limit")


def retained_charge(call: Charge, currency: str | None) -> str:
    """Retain uncertain charge bounds; use actual completed costs if known."""
    if call.currency != currency:
        raise ValueError("budget_currency_mismatch")
    usage = call.usage
    reported = usage.cost_decimal if usage is not None else None
    if (
        usage is not None
        and reported is not None
        and usage.currency != currency
    ):
        raise ValueError("budget_currency_mismatch")
    if call.state == "completed" and reported is not None:
        return reported
    reserved = call.cost_reservation_decimal
    if reserved is None:
        raise ValueError("budget_charge_unavailable")
    return (
        str(max(Decimal(reserved), Decimal(reported)))
        if reported is not None
        else reserved
    )


def sum_decimal(values: list[str]) -> str:
    """Add reported decimal charges without binary floating point rounding."""
    amounts = [Decimal(value) for value in values]
    with localcontext() as context:
        context.prec = (
            max((len(a.as_tuple().digits) for a in amounts), default=1) + 32
        )
        context.traps[Inexact] = True
        try:
            return str(sum(amounts, Decimal(0)))
        except DecimalException as exc:
            raise ValueError("budget_charge_unavailable") from exc


def aggregate(calls: list[Charge]) -> Usage:
    """Keep unreported and unresolved usage null across every call."""
    usages = [call.usage for call in calls if call.state == "completed"]
    data: dict[str, int | str | None] = {}
    for field in ("input_tokens", "output_tokens", "cached_tokens"):
        values = [
            getattr(usage, field) if usage is not None else None
            for usage in usages
        ]
        data[field] = (
            sum(value for value in values if value is not None)
            if len(values) == len(calls) and None not in values
            else None
        )
    costs = [
        usage.cost_decimal if usage is not None else None for usage in usages
    ]
    currencies = {
        usage.currency if usage is not None else None for usage in usages
    }
    known = (
        bool(calls)
        and len(usages) == len(calls)
        and None not in costs
        and len(currencies) == 1
        and None not in currencies
    )
    data["cost_decimal"] = (
        sum_decimal([value for value in costs if value is not None])
        if known
        else None
    )
    data["currency"] = (
        next(iter(currencies))
        if len(usages) == len(calls)
        and len(currencies) == 1
        and None not in currencies
        else None
    )
    return Usage.model_validate(data)
