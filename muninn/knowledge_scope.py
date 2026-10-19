"""Compare source scope applicability without granting admission authority."""

from __future__ import annotations

from muninn.knowledge_models import Scope


def scope_applies(source: Scope, target: Scope) -> bool:
    """Allow a matching project scope to narrow to a task, never widen."""
    return source.project_id == target.project_id and (
        source.task is None or source.task == target.task
    )
