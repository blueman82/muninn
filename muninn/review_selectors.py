"""Project provider fields with original block identity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from muninn import classify
from muninn.event_model import TEXT_LIMIT, as_record, finish
from muninn.knowledge_models import Provider, Scope
from muninn.redaction import redact
from muninn.review_authority import direct_speech
from muninn.review_evidence_models import (
    Authority,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSelector,
    EvidenceType,
    OriginalLocator,
)
from muninn.review_json import (
    canonical_json,
    finite_float,
    reject_constant,
    unique_object,
)

PROJECTION_VERSION = "permitted-fields-v2"
SOURCE_VERSIONS = {
    "codex": "codex-response-item-v1",
    "claude": "claude-content-block-v1",
    "cursor": "cursor-visible-bubble-v1",
}
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 4096
MAX_SCAN_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Field:
    """Describe a visible field without granting authority to its contents."""

    part: int
    pointer: str
    text: str
    evidence_type: EvidenceType
    role: str
    status: str | None = None


def decode(raw: bytes) -> dict[str, Any]:
    """Decode a bounded strict original without permitting duplicate keys."""
    if not raw or len(raw) > MAX_RECORD_BYTES:
        raise ValueError("evidence_limit")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("invalid_original")
    return cast(dict[str, Any], value)


def _text_fields(
    value: object, prefix: str, part: int, kind: EvidenceType, role: str
) -> list[Field]:
    """Project scalar text or typed visible text blocks only."""
    if isinstance(value, str):
        return [Field(part, prefix, value, kind, role)]
    fields: list[Field] = []
    if isinstance(value, list):
        for i, item in enumerate(cast(list[Any], value)):
            block = as_record(item)
            if (
                block is not None
                and block.get("type") in ("text", "input_text", "output_text")
                and isinstance(block.get("text"), str)
            ):
                fields.append(
                    Field(
                        part, f"{prefix}/{i}/text", block["text"], kind, role
                    )
                )
    return fields


def _codex(record: dict[str, Any]) -> list[Field]:
    """Permit known response shapes, excluding private replies."""
    payload = as_record(record.get("payload"))
    if record.get("type") != "response_item" or payload is None:
        return []
    kind = payload.get("type")
    if kind == "message":
        role = payload.get("role")
        if role not in ("user", "assistant") or payload.get("channel") not in (
            None,
            "final",
        ):
            return []
        evidence: EvidenceType = (
            "owner_statement" if role == "user" else "assistant_report"
        )
        return _text_fields(
            payload.get("content"), "/payload/content", 1, evidence, role
        )
    if kind in ("function_call", "custom_tool_call"):
        key = "arguments" if kind == "function_call" else "input"
        return _text_fields(
            payload.get(key), f"/payload/{key}", 1, "tool_request", "assistant"
        )
    if kind in ("function_call_output", "custom_tool_call_output"):
        return _text_fields(
            payload.get("output"), "/payload/output", 1, "tool_result", "user"
        )
    return []


def _claude_block(block: dict[str, Any], part: int, role: str) -> list[Field]:
    """Select only fields inside the original absolute block position."""
    prefix = f"/message/content/{part - 1}"
    kind = block.get("type")
    if kind == "text":
        evidence: EvidenceType = (
            "owner_statement" if role == "user" else "assistant_report"
        )
        return _text_fields(
            block.get("text"), f"{prefix}/text", part, evidence, role
        )
    if kind == "tool_result" and role == "user":
        fields = _text_fields(
            block.get("content"),
            f"{prefix}/content",
            part,
            "tool_result",
            role,
        )
        status = "reported_error" if block.get("is_error") is True else None
        return [
            Field(f.part, f.pointer, f.text, f.evidence_type, f.role, status)
            for f in fields
        ]
    if kind == "tool_use" and role == "assistant":
        inputs = as_record(block.get("input"))
        if block.get("name") in ("Bash", "exec_command") and isinstance(
            inputs, dict
        ):
            key = "command" if block["name"] == "Bash" else "cmd"
            return _text_fields(
                inputs.get(key),
                f"{prefix}/input/{key}",
                part,
                "tool_request",
                role,
            )
    return []


def _claude(record: dict[str, Any]) -> list[Field]:
    """Project known Claude messages using physical block ordinals."""
    role = record.get("type")
    message = as_record(record.get("message"))
    if role not in ("user", "assistant") or message is None:
        return []
    if message.get("role") not in (None, role):
        return []
    content = message.get("content")
    if isinstance(content, str):
        if role == "assistant" and message.get("stop_reason") not in (
            None,
            "end_turn",
            "stop_sequence",
        ):
            return []
        kind: EvidenceType = (
            "owner_statement" if role == "user" else "assistant_report"
        )
        return _text_fields(content, "/message/content", 1, kind, role)
    if not isinstance(content, list):
        return []
    final = role != "assistant" or message.get("stop_reason") in (
        None,
        "end_turn",
        "stop_sequence",
    )
    return [
        field
        for i, block in enumerate(cast(list[Any], content), 1)
        if isinstance(block, dict)
        for field in _claude_block(cast(dict[str, Any], block), i, role)
        if final or field.evidence_type != "assistant_report"
    ]


def cursor_materialized(record: dict[str, Any]) -> bool:
    """Match imported bubble eligibility before evidence field filtering."""
    text = record.get("text")
    if (
        record.get("type") not in (1, 2)
        or not isinstance(text, str)
        or not text
    ):
        return False
    try:
        finish(text, TEXT_LIMIT)[0].encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def fields(provider: Provider, record: dict[str, Any]) -> list[Field]:
    """Return the finite registry of permitted fields for a record."""
    if provider == "codex":
        return _codex(record)
    if provider == "claude":
        return _claude(record)
    kind = record.get("type")
    if type(kind) is int and kind in (1, 2):
        role = "user" if kind == 1 else "assistant"
        evidence: EvidenceType = (
            "owner_statement" if kind == 1 else "assistant_report"
        )
        return _text_fields(record.get("text"), "/text", 1, evidence, role)
    return []


def selected(
    fields: list[Field], part: int, selector: EvidenceSelector
) -> tuple[Field, str]:
    """Require an exact registered pointer, part and decoded Unicode span."""
    matching = [
        f
        for f in fields
        if f.part == part and f.pointer == selector.json_pointer
    ]
    if len(matching) != 1:
        raise ValueError("forbidden_selector")
    field = matching[0]
    start = selector.start if selector.start is not None else 0
    end = selector.end if selector.end is not None else len(field.text)
    if end > len(field.text) or start >= end:
        raise ValueError("invalid_span")
    # Redact before slicing too: a span through the middle of a credential
    # must not evade a pattern which needs the credential's prefix.
    safe, changed = redact(field.text)
    if changed and (selector.start is not None or selector.end is not None):
        raise ValueError("redacted_span")
    excerpt = safe[start:end]
    if not excerpt.strip():
        raise ValueError("empty_evidence")
    return field, excerpt


def authority(
    field: Field,
    context: list[Field],
    record: dict[str, Any],
    primary: bool,
    selector: EvidenceSelector | None = None,
) -> Authority:
    """Make a conservative contextual judgment, not from role alone."""
    if field.evidence_type == "assistant_report":
        return "reported"
    if field.evidence_type != "owner_statement" or not primary:
        return "untrusted"
    text = "\n".join(
        f.text for f in context if f.evidence_type == "owner_statement"
    ).strip()
    if any(f.evidence_type == "tool_result" for f in context):
        return "untrusted"
    start = selector.start if selector and selector.start is not None else 0
    end = (
        selector.end
        if selector and selector.end is not None
        else len(field.text)
    )
    return (
        "direct_owner"
        if direct_speech(text, field.text, (start, end), record)
        else "untrusted"
    )


def _scope(conn: sqlite3.Connection, source_id: int, line: int) -> Scope:
    """Resolve the preserved mapped scope key without probing a repository."""
    row = conn.execute(
        "SELECT sc.key,sc.kind FROM event e JOIN scope sc ON sc.id=e.scope_id"
        " WHERE e.source_id=? AND e.line<=? ORDER BY e.line DESC,e.part "
        "DESC LIMIT 1",
        (source_id, line),
    ).fetchone()
    if row is None:
        raise ValueError("scope_unavailable")
    return Scope(
        project_id=None if row["kind"] == "global" else row["key"], task=None
    )


def check_tombstones(
    conn: sqlite3.Connection, source: sqlite3.Row, locator: EvidenceLocator
) -> None:
    """Fail closed on erased originals, including erased fork families."""
    family = conn.execute(
        "WITH RECURSIVE family(thread,parent,known) AS (SELECT "
        "thread_id,forked_from_id,id FROM source WHERE id=? UNION SELECT "
        "f.parent,s.forked_from_id,s.id FROM family f LEFT JOIN source s "
        "ON s.thread_id=f.parent AND s.provider=? "
        "WHERE f.parent IS NOT NULL) SELECT thread,known FROM family "
        "LIMIT 129",
        (source["id"], locator.source.provider),
    ).fetchall()
    if len(family) > 128:
        raise ValueError("evidence_limit")
    for row in family:
        erased = conn.execute(
            "SELECT 1 FROM tombstone WHERE provider=? AND ((level='session' "
            "AND session_root=?) OR (level='thread' AND thread_id=?) OR "
            "(level='line' AND thread_id=? AND (line_sha256=? OR line=0))) "
            "LIMIT 1",
            (
                locator.source.provider,
                source["session_root"],
                row[0],
                row[0],
                locator.line_sha256,
            ),
        ).fetchone()
        # Content erasure may have removed the only materialized role/context
        # needed to prove a safe projection; ambiguity must not resurrect it.
        if erased:
            raise ValueError("evidence_erased")
        if row[1] is None:
            raise ValueError("ancestry_unavailable")


def build_record(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    locator: EvidenceLocator,
    selector: EvidenceSelector,
    raw: bytes,
    original: OriginalLocator,
) -> EvidenceRecord:
    """Build a governed selection with checked identity and context."""
    if classify.record_hash(raw) != locator.line_sha256:
        raise ValueError("evidence_changed")
    check_tombstones(conn, source, locator)
    record = decode(raw)
    permitted = fields(locator.source.provider, record)
    field, excerpt = selected(permitted, locator.part, selector)
    scope = _scope(conn, source["id"], locator.line)
    if locator.source.provider == "cursor":
        indexed = conn.execute(
            "SELECT line_sha256 FROM event WHERE source_id=? AND line=? AND "
            "part=1",
            (source["id"], locator.line),
        ).fetchone()
        if indexed is None or indexed[0] != locator.line_sha256:
            raise ValueError("evidence_changed")
    return EvidenceRecord(
        id=f"E_{uuid4().hex}",
        provider=locator.source.provider,
        thread_id=locator.source.thread_id,
        session_root=source["session_root"],
        line=locator.line,
        part=locator.part,
        line_sha256=locator.line_sha256,
        source_version=SOURCE_VERSIONS[locator.source.provider],
        projection_version=PROJECTION_VERSION,
        original_locator=original,
        selector=selector,
        selector_digest=hashlib.sha256(
            canonical_json(selector).encode()
        ).hexdigest(),
        evidence_type=field.evidence_type,
        authority=authority(field, permitted, record, True, selector),
        scope=scope,
        excerpt=excerpt,
        state="live",
        selected_at=time.time(),
    )
