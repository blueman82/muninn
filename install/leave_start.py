"""Leave one unfinished first-policy upgrade start without undoing it.

A later upgrade takes over the links and provider entries that the start
wrote, as it does for a start that did not verify. The leave writes one
mark into the kept rollback record. It removes no file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tomllib
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

from install import (
    codex_interface_recovery,
    host_renewal_recovery,
    legacy_state,
    policy_records,
    record,
    review_policy,
)
from install.context import Ctx, StepFailedError, link_text
from install.record import Record
from muninn import store

MARK = "left_unfinished"
QUESTION = "Leave this unfinished install? [y/N] "


def leave(ctx: Ctx, prompt: Callable[[str], str] | None) -> str:
    """Mark one unfinished upgrade start as left after explicit consent.

    Args:
        ctx: The context of this run. ``dry_run`` shows only the plan.
        prompt: Asks the person one question, or None without a terminal.

    Returns:
        ``nothing``, ``would_leave`` or ``left``.

    Raises:
        StepFailedError: If the start is not supported, it changed
            something that must be undone, or the person did not agree.
    """
    found = _find(ctx)
    if found is None:
        ctx.say("Muninn found no unfinished install to leave.")
        return "nothing"
    active, rec = found
    _prove(active, rec)
    _plan(ctx, rec)
    if ctx.dry_run:
        ctx.say(
            "DRY-RUN: Muninn changed no file. To leave the install, run"
            " bin/muninn-install --leave-unfinished."
        )
        return "would_leave"
    _confirm(prompt)
    with legacy_state.operation(ctx) as check:
        again = _find(ctx)
        if again is None or again[0].rdir != active.rdir or again[1] != rec:
            raise StepFailedError("leave_record_changed")
        _prove(active, rec)
        check()
        record.save(active, {**rec, "superseded_by": MARK})
    ctx.say(
        "Muninn left the unfinished install. Run bin/muninn-install again"
        " to install Muninn."
    )
    return "left"


def _find(ctx: Ctx) -> tuple[Ctx, Record] | None:
    """Find the one unfinished start that this command can leave.

    Args:
        ctx: The context of this run.

    Returns:
        The context and the record of the start, or None.

    Raises:
        StepFailedError: If a different kind of record is pending.
    """
    if ctx.platform not in ("darwin", "linux"):
        raise StepFailedError("leave_record_unsupported")
    if (
        legacy_state.pending(ctx) is not None
        or host_renewal_recovery.pending(ctx) is not None
        or codex_interface_recovery.pending(ctx) is not None
    ):
        raise StepFailedError("leave_record_unsupported")
    found = review_policy.pending(ctx)
    if found is None:
        return None
    rec = found[1]
    if (
        rec.get("upgrade") is not True
        or rec.get("fresh") is not False
        or "host_renewal" in rec
        or "codex_interface" in rec
    ):
        raise StepFailedError("leave_record_unsupported")
    return found


def _prove(active: Ctx, rec: Record) -> None:
    """Refuse when the start changed something that must be undone.

    Args:
        active: The context of the unfinished start.
        rec: Its rollback record.

    Raises:
        StepFailedError: For a committed policy, a changed link, or an
            unreadable provider file.
    """
    _no_policy(active, rec)
    _links(active, rec)
    _providers(active)


def _no_policy(active: Ctx, rec: Record) -> None:
    """Require that the start committed no first policy.

    Args:
        active: The context of the unfinished start.
        rec: Its rollback record.

    Raises:
        StepFailedError: If a policy is active or the store is unreadable.
    """
    if (
        policy_records.activation_phase(rec) != "intent"
        or rec.get("policy_status") == "active"
    ):
        raise StepFailedError("leave_policy_active")
    try:
        with closing(store.connect_ro(store.db_path(active.data))) as conn:
            row = conn.execute(
                "SELECT admission,policy_json FROM review_control"
            ).fetchone()
    except (store.StoreUnavailableError, sqlite3.Error, OSError):
        raise StepFailedError("leave_policy_active") from None
    if row is None or row[0] != 0 or row[1] is not None:
        raise StepFailedError("leave_policy_active")


def _links(active: Ctx, rec: Record) -> None:
    """Require each link at its before-value or at the value of the start.

    Args:
        active: The context of the unfinished start.
        rec: Its rollback record.

    Raises:
        StepFailedError: If a link has a third value or the release of
            the start is missing.
    """
    if not isinstance(rec.get("links"), dict) or not isinstance(
        rec.get("python"), dict
    ):
        raise StepFailedError("leave_links_changed")
    links = cast(dict[str, object], rec["links"])
    python = cast(dict[str, object], rec["python"])
    pinned: dict[str, object] = {
        str(active.lib / "current"): rec["sha"],
        str(active.muninn): str(active.lib / "current/bin/muninn"),
        str(active.lib / "python"): python.get("path"),
    }
    for name, before in links.items():
        path = Path(name)
        if path.is_symlink():
            now = link_text(path)
        elif os.path.lexists(path):
            raise StepFailedError("leave_links_changed")
        else:
            now = None
        if now != before and (name not in pinned or now != pinned[name]):
            raise StepFailedError("leave_links_changed")
    release = active.lib / rec["sha"]
    if release.is_symlink() or not release.is_dir():
        raise StepFailedError("leave_links_changed")


def _providers(active: Ctx) -> None:
    """Require provider files that a later upgrade can read.

    Args:
        active: The context of the unfinished start.

    Raises:
        StepFailedError: If a provider file exists and cannot be parsed.
    """
    for path in (active.settings, active.cursor_settings):
        if not os.path.lexists(path):
            continue
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError, RecursionError):
            raise StepFailedError("leave_provider_unreadable") from None
        if not isinstance(value, dict):
            raise StepFailedError("leave_provider_unreadable")
    if os.path.lexists(active.config):
        try:
            tomllib.loads(active.config.read_bytes().decode())
        except (OSError, ValueError, RecursionError):
            raise StepFailedError("leave_provider_unreadable") from None


def _plan(ctx: Ctx, rec: Record) -> None:
    """Say what Muninn found and what the leave does, with no path.

    Args:
        ctx: The context of this run.
        rec: The record of the unfinished start.
    """
    ctx.say(
        f"Muninn found an unfinished upgrade from {rec['ts']}, for commit"
        f" {rec['sha'][:7]}."
    )
    ctx.say(
        "Muninn will mark it as left. It keeps the install record, and it"
        " removes no file."
    )
    ctx.say(
        "It changes no link, no settings file and no service. Learning"
        " stays off."
    )
    ctx.say(
        "The next bin/muninn-install takes over the links and the hook"
        " entries of that upgrade."
    )


def _confirm(prompt: Callable[[str], str] | None) -> None:
    """Require an explicit yes from the person.

    Args:
        prompt: Asks the person one question, or None without a terminal.

    Raises:
        StepFailedError: If there is no terminal or the answer is not yes.
    """
    if prompt is None:
        raise StepFailedError("leave_confirmation_required")
    try:
        answer = prompt(QUESTION)
    except (EOFError, OSError):
        raise StepFailedError("leave_confirmation_required") from None
    if answer.strip().lower() not in ("y", "yes"):
        raise StepFailedError("leave_confirmation_required")
