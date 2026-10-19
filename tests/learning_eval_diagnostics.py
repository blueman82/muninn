"""Bounded synthetic pilot failure diagnostics without transcript text."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from tools.learning_eval.pilot_models import PilotResult


def launch_diagnostic(directory: Path, result: PilotResult) -> dict[str, Any]:
    """Show synthetic trial and account phases after a count failure.

    Args:
        directory: Isolated synthetic pilot directory.
        result: Completed synthetic pilot result.

    Returns:
        Trial identifiers and account lifecycle codes without source text.
    """
    path = directory / "account" / "account.sqlite3"
    with closing(
        sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    ) as conn:
        calls = [
            json.loads(row[0])
            for row in conn.execute("SELECT record FROM calls ORDER BY rowid")
        ]
        scheduled = [
            (
                json.loads(record)["trial_id"],
                json.loads(record)["purpose"],
                outcome,
            )
            for record, outcome in conn.execute(
                "SELECT record,outcome FROM scheduled ORDER BY rowid"
            )
        ]
    return {
        "trials": [
            (
                trial.case_id,
                trial.comparator,
                trial.outcome,
                len(trial.receipts),
                trial.call_ids,
            )
            for trial in result.trials
        ],
        "calls": [
            (
                call["schedule"]["trial_id"],
                call["schedule"]["purpose"],
                call["state"],
                call["outcome"],
            )
            for call in calls
        ],
        "scheduled": scheduled,
    }
