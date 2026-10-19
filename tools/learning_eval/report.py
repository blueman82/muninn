"""Write strict offline reports with explicit null measurements."""

from __future__ import annotations

from pathlib import Path

from muninn.review_json import canonical_json
from tools.learning_eval.models import EvaluationReport


def write_report(report: EvaluationReport, path: Path) -> None:
    """Write a validated JSON report including all nullable fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report) + "\n", encoding="utf-8")
