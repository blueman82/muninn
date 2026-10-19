"""Define the fixed data-only later-task answer and bounded prompt."""

from __future__ import annotations

import json

from muninn.knowledge_models import StrictModel
from muninn.review_rounds import round_schema
from tools.learning_eval.cases import strict_json


class TaskAnswer(StrictModel):
    """Return only an answer, without operations or learning authority."""

    answer: str


def task_schema() -> dict[str, object]:
    """Generate the sole program-owned task result schema."""
    return TaskAnswer.model_json_schema()


def protocol_schema(protocol: str) -> dict[str, object]:
    """Select only fixed program-owned schemas."""
    if protocol == "muninn-review-round-v1":
        return round_schema()
    if protocol == "muninn-task-answer-v1":
        return task_schema()
    raise ValueError("protocol_mismatch")


def parse_answer(raw: bytes, limit: int) -> TaskAnswer:
    """Validate bounded decoded output after containment has completed."""
    if type(raw) is not bytes or len(raw) > limit:
        raise ValueError("task_output_limit")
    try:
        strict_json(raw)
        return TaskAnswer.model_validate_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_task_answer") from None


def task_prompt(
    instructions: str, query: str, knowledge: object, limit: int
) -> bytes:
    """Keep trusted instructions separate from task and retrieved data."""
    raw = json.dumps(
        {
            "protocol": "muninn-task-answer-v1",
            "instructions": instructions,
            "query": query,
            "knowledge": knowledge,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(raw) > limit:
        raise ValueError("task_prompt_limit")
    return raw
