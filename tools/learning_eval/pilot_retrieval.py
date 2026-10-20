"""Observe actual eligible lexical, context and guide-body exposure."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

from muninn import knowledge_push, knowledge_read
from muninn.knowledge_models import GuideBody, KnowledgeView, Scope
from muninn.review_evidence import validate_evidence
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import ReadBudget
from muninn.review_supervisor_store import database
from muninn.tombstones import require_current_tombstones
from tools.learning_eval.cases import digest
from tools.learning_eval.pilot_models import Exposure, ExposureRecipe


def _content(value: object) -> object:
    """Compare learned state without random IDs and support identity labels."""
    if isinstance(value, dict):
        return {
            key: _content(item)
            for key, item in cast(dict[str, object], value).items()
            if key not in {"evidence_refs", "id", "created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_content(item) for item in cast(list[object], value)]
    return value


def observe(
    home: Path,
    scope: Scope,
    query: str,
    recipe: ExposureRecipe,
    policy: ReviewPolicy,
    *,
    withheld: bool,
) -> Exposure:
    """Apply current support and physical-original checks before exposure."""
    now = time.time()
    with database(home, transaction=False) as conn:
        require_current_tombstones(conn, home)
        budget = ReadBudget(policy.max_input_bytes)

        def admitted(ident: str, revision: int) -> bool:
            try:
                ids = knowledge_read.knowledge_support(
                    conn, ident, revision, scope, now
                )
                validate_evidence(
                    conn, home, ids, scope, policy, budget=budget
                )
            except ValueError:
                return False
            return True

        context = knowledge_push.knowledge_context(
            conn, scope, recipe.context_bytes, now
        )
        notes = [v for v in context.notes if admitted(v.id, v.revision)]
        guides = [v for v in context.guides if admitted(v.id, v.revision)]
        hits = [
            v
            for v in knowledge_read.search_knowledge(conn, query, scope)[
                : recipe.search_limit
            ]
            if admitted(v.id, v.revision)
        ]
        discovered = {(v.id, v.revision) for v in (*guides, *hits)}
        opened: list[KnowledgeView] = []
        omitted: list[str] = []
        used = 0
        for ident, revision in sorted(discovered):
            if len(opened) >= recipe.guide_open_limit:
                break
            if not admitted(ident, revision):
                omitted.append(ident)
                continue
            view = knowledge_read.show_knowledge(conn, ident, revision)
            if not isinstance(view.body, GuideBody):
                continue
            size = len(canonical_json(view).encode())
            if used + size > recipe.guide_bytes:
                omitted.append(ident)
                continue
            used += size
            opened.append(view)
        learned = [
            v
            for v in knowledge_read.current_knowledge(conn, scope, now)
            if admitted(v.id, v.revision)
        ]
    # Guide bodies enter the prompt only after a separate admitted open.
    summaries = [
        {
            "id": v.id,
            "revision": v.revision,
            "title": v.body.title if isinstance(v.body, GuideBody) else None,
        }
        for v in hits
    ]
    content: dict[str, object] = (
        {}
        if withheld
        else {
            "notes": [v.model_dump(mode="json") for v in notes],
            "guides": [v.model_dump(mode="json") for v in guides],
            "search": summaries,
            "opened": [v.model_dump(mode="json") for v in opened],
        }
    )
    normalized = [
        _content(v.body.model_dump(mode="json"))
        for v in learned
        if v.body is not None
    ]
    return Exposure(
        note_ids=tuple(v.id for v in notes),
        guide_ids=tuple(v.id for v in guides),
        fts_ids=tuple(v.id for v in hits),
        opened_ids=tuple(v.id for v in opened),
        omitted_ids=tuple(omitted),
        context_truncated=context.truncated,
        query=query,
        prompt_context=content,
        learned_digest=digest(
            sorted(normalized, key=lambda v: json.dumps(v, sort_keys=True))
        ),
    )
