"""Build explicitly finite synthetic authorization for capability tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from muninn.review_models import ReviewContext, require_pilot_budget
from muninn.review_runner import RunnerCapability
from tools.learning_eval.models import TrialAuthorization


def authorization(
    context: ReviewContext,
    capability: RunnerCapability,
    raw: bytes,
    home: Path,
) -> TrialAuthorization:
    """Bind the fixture's exact candidate to a finite synthetic budget."""
    return TrialAuthorization(
        assessment_id="synthetic",
        candidate_digest=hashlib.sha256(raw).hexdigest(),
        config_digest=capability.config_digest,
        budget=require_pilot_budget(context.policy.budget),
        disposable_home=home,
        approved_by="synthetic-owner",
        approved_at=1.0,
    )
