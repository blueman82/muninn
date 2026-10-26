"""Ask for learning scope and apply it before the service restarts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing

from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import review_history, review_state, store
from muninn.review_models import ReviewPolicy


def choose(
    ctx: Ctx, requested: str | None, prompt: Callable[[str], str]
) -> None:
    """Ask once before installation changes when no saved choice exists."""
    ctx.learning_prompt = prompt
    if ctx.dry_run:
        ctx.say(
            "Learning scope: choose From now or All available "
            "history during installation."
        )
        return
    if store.db_path(ctx.data).is_file():
        try:
            with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
                saved = review_state.selection(conn)
                control = conn.execute(
                    "SELECT admission,policy_json FROM review_control"
                ).fetchone()
                if (
                    control is not None
                    and control["policy_json"] is not None
                    and not control["admission"]
                ):
                    ctx.say(
                        "Learning remains paused. The upgrade will "
                        "preserve this pause."
                    )
                    return
            if saved is not None and requested is None:
                ctx.say("Learning scope retained: " + saved.mode + ".")
                return
        except store.StoreUnavailableError:
            pass
    if ctx.platform != "darwin" and ctx.review_policy is None:
        ctx.say("Learning needs a supported runner policy on this platform.")
        return
    value = requested
    if value is None:
        ctx.say(
            "From now: learn from records created after this install "
            "or upgrade."
        )
        ctx.say(
            "All available history: review verified older records "
            "and future records."
        )
        ctx.say(
            "History can take time and use your provider allowance. "
            "Subscription cost is unknown."
        )
        try:
            value = {"n": "now", "a": "all", "now": "now", "all": "all"}.get(
                prompt(
                    "Choose n for From now or a for All available history: "
                )
                .strip()
                .lower()
            )
        except EOFError:
            value = None
    if value not in ("now", "all"):
        raise StepFailedError(
            "learning_scope_required: choose --learning-scope now or "
            "--learning-scope all"
        )
    ctx.learning_mode = value


def _summary(ctx: Ctx, selected: review_history.HistoryScan) -> None:
    """Show estimates and exact excluded ranges before confirmation."""
    if selected.estimate["seconds_low"] is None:
        ctx.say(
            "Time estimate unavailable: "
            "no comparable completed calls were recorded."
        )
    else:
        ctx.say(
            f"Estimated seconds: {selected.estimate['seconds_low']} "
            f"to {selected.estimate['seconds_high']}."
        )
        ctx.say(
            f"Estimate confidence: {selected.estimate['confidence']}. "
            f"Sample calls: {selected.estimate['sample_calls']}."
        )
    for item in selected.excluded[:10]:
        ctx.say(
            f"Skipped {item['provider']}:{item['thread_id']}, "
            f"lines {item['from_line']}-{item['through_line']}: "
            f"{item['cause']}"
        )


def apply(ctx: Ctx, rec: Record, conn: sqlite3.Connection) -> None:
    """Use the caller's maintenance writer after policy activation."""
    if ctx.learning_mode is None:
        return
    control = conn.execute(
        "SELECT admission,policy_json FROM review_control"
    ).fetchone()
    if control is None or control["policy_json"] is None:
        ctx.say(
            "Learning scope is pending because no supported policy "
            "is installed."
        )
        return
    if not control["admission"]:
        ctx.say("Learning remains paused. The upgrade preserved this pause.")
        return
    policy = ReviewPolicy.model_validate_json(control["policy_json"])
    confirmation = None
    if ctx.learning_mode == "all":
        selected = review_history.scan(conn, policy, None)
        ctx.say(
            "History scope: all indexed projects. Global records "
            "stay excluded."
        )
        ctx.say(
            f"Eligible review requests: {len(selected.items)}. "
            f"Excluded source ranges: {len(selected.excluded)}."
        )
        _summary(ctx, selected)
        ctx.say("Subscription allowance and subscription cost are unknown.")
        if selected.estimate["api_cost_decimal"] is not None:
            ctx.say(
                f"Reported-cost estimate: "
                f'{selected.estimate["api_cost_decimal"]} '
                f'{selected.estimate["currency"]}.'
            )
        try:
            answer = (
                ""
                if ctx.learning_prompt is None
                else ctx.learning_prompt(
                    "Review this verified history? [yes/no] "
                )
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("yes", "y"):
            raise StepFailedError(
                "history_not_confirmed: no history reviews were queued"
            )
        confirmation = selected.digest
    conn.execute("BEGIN IMMEDIATE")
    try:
        rec["learning_scope"] = review_history.configure(
            conn,
            policy,
            ctx.learning_mode,
            None,
            float(ctx.now()),
            confirmation,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    ctx.say(
        f"Learning scope saved: {ctx.learning_mode}. Routine review "
        f"recovery is automatic."
    )
