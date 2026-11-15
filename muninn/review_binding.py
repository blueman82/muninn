"""Bind owner-required claims to verbatim sentences of trusted lines."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from muninn import classify
from muninn.knowledge_models import Body, EvidenceId, NoteBody
from muninn.knowledge_write import claims
from muninn.redaction import redact
from muninn.review_authority import SENTENCE, whole_sentences
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    SourceRef,
)
from muninn.review_originals import originals
from muninn.review_progress import source_row
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import (
    MAX_SCAN_BYTES,
    check_tombstones,
    decode,
    fields,
)

type Adjacent = Callable[[EvidenceRecord, EvidenceRecord], bool]


def _normal(text: str) -> str:
    """Collapse whitespace and ignore one final terminator."""
    collapsed = " ".join(text.split())
    return collapsed[:-1] if collapsed[-1:] in (".", "!", ";") else collapsed


def verbatim(unit: str, excerpt: str) -> bool:
    """Test a claim line against whole consecutive sentences of an excerpt.

    Args:
        unit: One claim line: note content, one condition or a guide claim.
        excerpt: The redacted evidence excerpt, itself whole sentences.

    Returns:
        Whether the line equals, case-sensitively, a run of complete
        sentences of the excerpt, so no negation or limit can be dropped.
    """
    # Excerpts are redacted, so compare the line as it will be stored.
    wanted = _normal(redact(unit)[0])
    bounds = [
        (m.start(), m.end())
        for m in SENTENCE.finditer(excerpt)
        if m[0].strip(" .!;\t\r\n")
    ]
    for i, (start, _) in enumerate(bounds):
        for _, end in bounds[i:]:
            found = _normal(excerpt[start:end])
            if found == wanted:
                return True
            if len(found) > len(wanted) + 1:
                break
    return False


def claim_lines(body: Body) -> list[tuple[list[str], list[EvidenceId]]]:
    """Return the lines of each supported claim with its references."""
    if isinstance(body, NoteBody):
        return [([body.content, *body.conditions], body.evidence_refs)]
    return [([text], refs) for _, text, refs, _ in claims(body)]


def bound(
    lines: list[str],
    refs: list[EvidenceId],
    records: dict[EvidenceId, EvidenceRecord],
    adjacent: Adjacent | None,
) -> bool:
    """Require every line to come from cited trusted words.

    Args:
        lines: Claim lines that need owner support.
        refs: Known evidence references cited by the claim.
        records: Revalidated evidence by identity.
        adjacent: Checks that an assistant record is the final permitted
            field directly before an owner record, or None to refuse pairs.

    Returns:
        Whether each line equals whole sentences of a cited owner line, or
        of a cited assistant reply directly before a cited owner reply.
    """
    cited = [records[r] for r in refs]
    owners = [
        r
        for r in cited
        if r.authority == "direct_owner"
        and r.evidence_type == "owner_statement"
    ]
    pending = [
        line
        for line in lines
        if not any(o.excerpt and verbatim(line, o.excerpt) for o in owners)
    ]
    if not owners or not pending:
        return bool(owners)
    if adjacent is None:
        return False
    paired = [
        r.excerpt
        for r in cited
        if r.evidence_type == "assistant_report"
        and r.excerpt
        and any(adjacent(r, o) for o in owners)
    ]
    return all(any(verbatim(line, e) for e in paired) for line in pending)


def adjacent_gap(
    conn: sqlite3.Connection,
    assistant: EvidenceRecord,
    owner: EvidenceRecord,
    budget: ReadBudget,
    *,
    whole_display: bool = False,
) -> bool:
    """Reread the gap and require no permitted field between two records.

    Args:
        conn: Caller-owned connection with the indexed source rows.
        assistant: Cited assistant record, before the owner record.
        owner: Cited owner record in the same thread.
        budget: The operation read allowance charged for the reread.
        whole_display: The caller already proved the assistant field is the
            whole and only field of its record, so it is not read again.

    Returns:
        Whether the assistant field is the last permitted field of its
        record, covers whole sentences, and no permitted field of any
        record lies between it and the owner record.

    Raises:
        ValueError: If the gap cannot be read or a gap line is erased.
        EvidenceLimitError: If the read allowance is exhausted.
    """
    if (
        assistant.evidence_type != "assistant_report"
        or owner.evidence_type != "owner_statement"
        or (assistant.provider, assistant.thread_id)
        != (owner.provider, owner.thread_id)
        or assistant.line >= owner.line
    ):
        return False
    first = assistant.line + 1 if whole_display else assistant.line
    if first == owner.line:
        return True
    source = source_row(conn, _thread(owner))
    records = originals(
        conn, source, first, owner.line - 1, MAX_SCAN_BYTES, budget=budget
    )
    for number, raw, _ in records:
        check_tombstones(
            conn,
            source,
            EvidenceLocator(
                source=_thread(owner),
                line=number,
                part=1,
                line_sha256=classify.record_hash(raw),
            ),
        )
    gap = [decode(raw) for _, raw, _ in records]
    if not whole_display:
        if classify.record_hash(records[0][1]) != assistant.line_sha256:
            return False
        if not _final_sentences(assistant, gap.pop(0)):
            return False
    return not any(fields(owner.provider, record) for record in gap)


def _final_sentences(
    assistant: EvidenceRecord, record: dict[str, Any]
) -> bool:
    """Require the last permitted field of a record, in whole sentences."""
    projected = fields(assistant.provider, record)
    pointer = assistant.selector.json_pointer
    at = [
        i
        for i, f in enumerate(projected)
        if f.part == assistant.part and f.pointer == pointer
    ]
    if len(at) != 1 or at[0] != len(projected) - 1:
        return False
    text = projected[-1].text
    start = assistant.selector.start or 0
    end = (
        assistant.selector.end
        if assistant.selector.end is not None
        else len(text)
    )
    return whole_sentences(text, (start, end))


def _thread(record: EvidenceRecord) -> SourceRef:
    """Return the thread reference of a record."""
    return SourceRef(provider=record.provider, thread_id=record.thread_id)
