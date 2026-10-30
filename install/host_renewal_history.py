"""Validate completed pointers while retaining their full historical rows."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from muninn.review_models import AttemptView
from muninn.review_request_views import request_view


def attempt_view(row: sqlite3.Row) -> AttemptView:
    """Parse one already streamed attempt without another database read."""
    data = dict(row)
    for key in ("requested", "observed", "usage"):
        raw = data.pop(key + "_json")
        data[key] = json.loads(cast(str, raw)) if raw is not None else None
    return AttemptView.model_validate_json(
        json.dumps({"schema_version": 1, **data})
    )


class CompletedHistory:
    """Require each retained completed pointer to resolve once to its owner."""

    def __init__(self) -> None:
        """Track only unresolved pointers from the existing request stream."""
        self.references: dict[str, str] = {}

    def add(self, row: sqlite3.Row) -> bool:
        """Recognize completed history without accepting duplicate pointers."""
        if row["state"] != "completed" or row["current_attempt_id"] is None:
            return False
        request = request_view(row)
        pointer = request.current_attempt_id
        if pointer is None or pointer in self.references:
            raise ValueError("host_renewal_job_invalid")
        self.references[pointer] = request.id
        return True

    def resolve(self, row: sqlite3.Row) -> None:
        """Validate the exact ended attempt while streaming all attempts."""
        if row["id"] not in self.references:
            return
        attempt = attempt_view(row)
        if (
            self.references.pop(attempt.id) != attempt.request_id
            or attempt.ended_at is None
        ):
            raise ValueError("host_renewal_job_invalid")
