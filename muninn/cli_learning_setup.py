"""Present explicit learning scope choices and verified history scans."""

from __future__ import annotations

import sqlite3
from argparse import Namespace

from muninn.review_history import configure, scan
from muninn.review_models import ReviewPolicy


def setup(
    conn: sqlite3.Connection, args: Namespace, policy: ReviewPolicy, now: float
) -> dict[str, object]:
    """Preview historical work or apply an explicitly confirmed choice."""
    if args.mode == "all" and args.confirm is None:
        selected = scan(conn, policy, args.project)
        return {
            "mode": "all",
            "scope": (
                "All indexed projects; global records are excluded."
                if args.project is None
                else args.project
            ),
            "eligible_requests": len(selected.items),
            "excluded_spans": len(selected.excluded),
            "excluded": selected.excluded,
            "estimate": selected.estimate,
            "confirmation": selected.digest,
            "next_step": "Run muninn learn setup --mode all --confirm "
            + selected.digest
            + (" --project " + args.project if args.project else ""),
        }
    return configure(conn, policy, args.mode, args.project, now, args.confirm)


def failure(code: str) -> dict[str, str]:
    """Explain setup refusals with one safe next action."""
    if code == "history_scan_changed":
        return {
            "error": code,
            "cause": "The verified history changed after the preview.",
            "action": "Preview the history again before confirming.",
        }
    if code == "learning_busy":
        return {
            "error": code,
            "cause": "A review is still active.",
            "action": "Wait for the active review before changing scope.",
        }
    return {
        "error": code,
        "cause": "Muninn could not apply the requested learning scope.",
        "action": "Check the scope options and the installed learning policy.",
    }
