"""Ask for learning scope and apply it before the service restarts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from install import learning_policy_recovery, policy_preview
from install.context import Ctx, StepFailedError
from install.policy_source_format import legacy_source, validate_policy_source
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
    saved, keep_paused = _existing(ctx, prompt)
    if keep_paused:
        return
    if saved is not None and requested is None:
        ctx.learning_mode = saved.mode
        ctx.retained_learning_scope = True
        ctx.say("Learning scope retained: " + saved.mode + ".")
        return
    if ctx.platform != "darwin" and ctx.review_policy is None:
        ctx.say("Learning needs a supported runner policy on this platform.")
        return
    ctx.say(
        "The default permits four simultaneous reviews and three "
        "total attempts per request."
    )
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


def _existing(
    ctx: Ctx, prompt: Callable[[str], str]
) -> tuple[review_state.LearningSelection | None, bool]:
    """Preserve pauses unless the person approves checked recovery."""
    if not store.db_path(ctx.data).is_file():
        return None, False
    if legacy_source(store.db_path(ctx.data)):
        return None, False
    try:
        with policy_preview.connection(store.db_path(ctx.data)) as conn:
            conn.row_factory = sqlite3.Row
            validate_policy_source(conn)
            return _saved_scope(ctx, prompt, conn)
    except (sqlite3.Error, store.StoreUnavailableError) as exc:
        raise StepFailedError("review_policy_state_invalid") from exc


def _saved_scope(
    ctx: Ctx, prompt: Callable[[str], str], conn: sqlite3.Connection
) -> tuple[review_state.LearningSelection | None, bool]:
    """Retain actual paused policy and selection from one guarded snapshot."""
    saved = review_state.selection(conn)
    control = conn.execute(
        "SELECT admission,policy_json FROM review_control"
    ).fetchone()
    if control is None or control["policy_json"] is None:
        return saved, False
    installed = ReviewPolicy.model_validate_json(control["policy_json"])
    allowed = (
        learning_policy_recovery.eligible(installed)
        or ctx.review_policy is not None
    )
    if ctx.enable_learning and not allowed:
        raise StepFailedError(
            "learning_custom_policy: supply a newly " "approved policy file"
        )
    if not control["admission"]:
        if not allowed:
            ctx.say("Learning remains paused under its custom policy.")
            return saved, True
        if not ctx.enable_learning:
            ctx.enable_learning = _consent(ctx, prompt)
            if not ctx.enable_learning:
                return saved, True
    if ctx.enable_learning:
        learning_policy_recovery.require_finished(conn)
    return saved, False


def _consent(ctx: Ctx, prompt: Callable[[str], str]) -> bool:
    """Ask separately before changing an existing learning pause."""
    ctx.say("Learning is paused under the existing policy.")
    ctx.say(
        "You can keep this pause or approve a checked restart "
        "with the new policy."
    )
    try:
        answer = prompt(
            "Enable learning after verifying that earlier calls "
            "finished? [yes/no] "
        )
    except EOFError:
        answer = ""
    accepted = answer.strip().lower() in ("yes", "y")
    if not accepted:
        ctx.say("Learning remains paused.")
    return accepted


def _summary(ctx: Ctx, selected: review_history.HistoryScan) -> None:
    """Show estimates and exact excluded ranges before confirmation."""
    if selected.estimate["seconds_low"] is None:
        ctx.say(
            "Time estimate unavailable: "
            "no comparable completed calls were recorded."
        )
    else:
        ctx.say(
            f"First-attempt estimate in seconds: "
            f"{selected.estimate['seconds_low']} "
            f"to {selected.estimate['seconds_high']}."
        )
        ctx.say(
            f"Estimate confidence: {selected.estimate['confidence']}. "
            f"Sample calls: {selected.estimate['sample_calls']}."
        )
    if selected.estimate["retry_seconds_high"] is not None:
        ctx.say(
            f"With permitted retries and backoffs: "
            f"{selected.estimate['retry_seconds_high']} seconds."
        )
    ctx.say("Estimates exclude setup waits and database delays.")
    for item in selected.excluded[:10]:
        ctx.say(
            f"Skipped {item['provider']}:{item['thread_id']}, "
            f"lines {item['from_line']}-{item['through_line']}: "
            f"{item['cause']}"
        )


def _confirm(
    ctx: Ctx, selected: review_history.HistoryScan, label: str
) -> str:
    """Require confirmation only when verified model work is present."""
    ctx.say(label)
    ctx.say(
        f"Eligible review requests: {len(selected.items)}. "
        f"Excluded source ranges: {len(selected.excluded)}."
    )
    _summary(ctx, selected)
    ctx.say("Subscription allowance and subscription cost are unknown.")
    if selected.estimate["api_cost_decimal"] is not None:
        ctx.say(
            f"First-attempt cost estimate: "
            f"{selected.estimate['api_cost_decimal']} "
            f"{selected.estimate['currency']}."
        )
        ctx.say(
            f"Retry-inclusive cost estimate: "
            f"{selected.estimate['retry_api_cost_decimal']} "
            f"{selected.estimate['currency']}."
        )
    if selected.items:
        try:
            answer = (
                ""
                if ctx.learning_prompt is None
                else ctx.learning_prompt(
                    "Review these verified records? [yes/no] "
                )
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("yes", "y"):
            raise StepFailedError(
                "history_not_confirmed: no history reviews were queued"
            )
    return selected.digest


def apply(ctx: Ctx, rec: Record, conn: sqlite3.Connection) -> None:
    """Use the caller's maintenance writer after policy activation."""
    if ctx.learning_mode is None:
        return
    control = conn.execute(
        "SELECT admission,policy_json FROM review_control"
    ).fetchone()
    if control is None or control["policy_json"] is None:
        ctx.say(
            "Learning scope is pending because no supported "
            "policy is installed."
        )
        return
    if not control["admission"]:
        ctx.say("Learning remains paused. The upgrade preserved this pause.")
        return
    policy = ReviewPolicy.model_validate_json(control["policy_json"])
    previous = review_state.selection(conn)
    project = (
        previous.project_id
        if previous is not None and ctx.retained_learning_scope
        else None
    )
    confirmation = None
    replacement_digest = None
    if ctx.learning_mode == "all":
        selected = review_history.scan(conn, policy, project)
        label = (
            "History scope: all indexed projects. Global records "
            "stay excluded."
            if project is None
            else "History scope: " + project
        )
        confirmation = _confirm(ctx, selected, label)
    elif ctx.retained_learning_scope and previous is not None:
        selected = review_history.replacements(conn, policy, previous)
        replacement_digest = _confirm(
            ctx,
            selected,
            "Queued reviews after your original From now start time.",
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = review_history.configure(
            conn,
            policy,
            ctx.learning_mode,
            project,
            float(ctx.now()),
            confirmation,
        )
        if replacement_digest is not None and previous is not None:
            result["enqueued"] = review_history.admit_replacements(
                conn, policy, previous, replacement_digest, float(ctx.now())
            )
        rec["learning_scope"] = result
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    ctx.say(
        f"Learning scope saved: {ctx.learning_mode}. Routine "
        f"review recovery is automatic."
    )
