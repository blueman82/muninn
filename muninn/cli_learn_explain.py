"""Explain retained learning outcomes without exposing source content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from muninn import review_codes, review_settings
from muninn.knowledge_models import RequestId
from muninn.review_request_views import (
    correction_status,
    request_row,
    request_view,
)


def _failure(conn: sqlite3.Connection, ident: str) -> str:
    """Explain the latest recorded attempt failure by its code alone."""
    attempt = conn.execute(
        "SELECT error_code FROM review_attempt WHERE request_id=? "
        "AND error_code IS NOT NULL ORDER BY started_at DESC,id DESC LIMIT 1",
        (ident,),
    ).fetchone()
    if attempt is None:
        return "No failure is recorded for this request."
    return f"Last failure ({attempt[0]}): {review_codes.explain(attempt[0])}"


def _impact(conn: sqlite3.Connection, ident: str) -> str:
    """Use the atomic receipt rather than infer partial proposal writes."""
    receipt = conn.execute(
        "SELECT applied_json,unresolved_json,outcome FROM review_receipt "
        "WHERE request_id=?",
        (ident,),
    ).fetchone()
    if receipt is None:
        return "Nothing from this request was saved as knowledge."
    return (
        f"The commit receipt records {len(json.loads(receipt[0]))} applied "
        f"knowledge changes and {len(json.loads(receipt[1]))} unresolved "
        f"items. Receipt outcome: {receipt[2]}."
    )


def explain(
    conn: sqlite3.Connection, ident: RequestId, home: Path
) -> dict[str, str]:
    """Return ordinary-language inspection using only retained metadata."""
    request = request_view(request_row(conn, ident))
    status = correction_status(conn, ident)
    paragraphs = [
        f"Learning request {ident} is currently {request.state}.",
        _failure(conn, ident),
        f"Correction status: {status}.",
        _impact(conn, ident),
        (
            "Learning is on."
            if review_settings.is_enabled(home)
            else "Learning is off. Run muninn learn on to turn it on."
        ),
    ]
    return {
        "request_id": ident,
        "correction_status": status,
        "explanation": "\n\n".join(paragraphs),
    }
