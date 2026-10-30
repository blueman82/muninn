"""Compare exact no-call outcomes with immutable historical retry limits."""

from __future__ import annotations

from collections.abc import Mapping

from install.host_renewal_models import ContainmentJob, ContainmentPlan
from muninn.knowledge_models import Digest

type RetryLimits = tuple[int, tuple[float, ...]]


def _job_match(
    old: ContainmentJob,
    new: ContainmentJob,
    limits: Mapping[Digest, RetryLimits],
) -> bool:
    """Compare one no-call job using only its validated historical schedule."""
    if (old.request.id, old.attempt.id, old.attempt_count) != (
        new.request.id,
        new.attempt.id,
        new.attempt_count,
    ):
        return False
    ended = new.attempt.ended_at
    if ended is None:
        return new == old
    expected_attempt = old.attempt.model_copy(
        update={
            "ended_at": ended,
            "progress_at": ended,
            "outcome": "abandoned",
        }
    )
    if new.attempt != expected_attempt or ended < old.attempt.progress_at:
        return False
    if old.request.state == "cancelled":
        expected_request = old.request
    else:
        schedule = limits.get(old.request.policy_digest)
        if schedule is None:
            return False
        maximum, backoffs = schedule
        count = old.attempt_count
        retry = ended + backoffs[count - 1] if count < maximum else None
        expected_request = old.request.model_copy(
            update={
                "state": "waiting" if retry is not None else "failed",
                "retry_at": retry,
                "current_attempt_id": None,
            }
        )
    return new.request == expected_request


def jobs_match(
    before: ContainmentPlan,
    after: tuple[ContainmentJob, ...],
    limits: Mapping[Digest, RetryLimits],
) -> bool:
    """Check exact named jobs against each immutable saved retry schedule.

    Args:
        before: Original named unfinished predecessor projection.
        after: Current views for those same jobs.
        limits: Only the selected snapshots' validated retry schedules.

    Returns:
        Whether all jobs are unchanged or have permitted abandoned shapes.
    """
    return len(before.jobs) == len(after) and all(
        _job_match(old, new, limits)
        for old, new in zip(before.jobs, after, strict=True)
    )
