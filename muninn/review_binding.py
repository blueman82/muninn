"""Find the full owner messages that prove an owner-required claim."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import ExitStack

from muninn import classify
from muninn.knowledge_models import EvidenceId
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceRecord,
    JsonlOriginal,
    SourceRef,
)
from muninn.review_originals import originals, reread
from muninn.review_progress import source_row
from muninn.review_read_budget import ReadBudget
from muninn.review_selectors import (
    MAX_SCAN_BYTES,
    check_tombstones,
    decode,
    fields,
    indexed_kind,
)

type Adjacent = Callable[[EvidenceRecord, EvidenceRecord], bool]


def full_field(record: EvidenceRecord) -> bool:
    """Report whether a record selects its whole field, with no span."""
    return record.selector.start is None and record.selector.end is None


def owner_supported(
    refs: list[EvidenceId],
    records: dict[EvidenceId, EvidenceRecord],
    adjacent: Adjacent | None,
) -> bool:
    """Require a full owner message as proof, with only paired replies.

    Args:
        refs: Known evidence references cited by the claim.
        records: Revalidated evidence by identity.
        adjacent: Checks that an assistant record is the final permitted
            field directly before an owner record, or None to refuse pairs.

    Returns:
        Whether the claim cites a full ``direct_owner`` message, no other
        user text, and each cited assistant reply is a full field directly
        before one of the owner messages.
        The model's claim text is not compared with the proof.
    """
    cited = [records[r] for r in refs]
    owners = [
        r
        for r in cited
        if r.authority == "direct_owner"
        and r.evidence_type == "owner_statement"
        and full_field(r)
    ]
    replies = [r for r in cited if r.evidence_type == "assistant_report"]
    # Any other cited user text, such as a hook line, would join the proof.
    others = [
        r
        for r in cited
        if r.evidence_type == "owner_statement" and r not in owners
    ]
    return (
        bool(owners)
        and not others
        and all(
            adjacent is not None
            and full_field(reply)
            and any(adjacent(reply, owner) for owner in owners)
            for reply in replies
        )
    )


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
        Whether the assistant line is an indexed reply whose field is the
        last permitted field of its record, and no permitted field of any
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
    source = source_row(conn, _thread(owner))
    # A fork or replay copy of a reply has no reply event of its own.
    if not indexed_kind(
        conn, source["id"], assistant.line, assistant.line_sha256, "reply"
    ):
        return False
    first = assistant.line + 1 if whole_display else assistant.line
    if first == owner.line:
        return True
    records = _between(conn, source, assistant, owner, first, budget)
    for number, raw in records:
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
    gap = [decode(raw) for _, raw in records]
    if not whole_display:
        if classify.record_hash(records[0][1]) != assistant.line_sha256:
            return False
        projected = fields(assistant.provider, gap.pop(0))
        if not projected or (projected[-1].part, projected[-1].pointer) != (
            assistant.part,
            assistant.selector.json_pointer,
        ):
            return False
    return not any(fields(owner.provider, record) for record in gap)


def _between(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    assistant: EvidenceRecord,
    owner: EvidenceRecord,
    first: int,
    budget: ReadBudget,
) -> list[tuple[int, bytes]]:
    """Read the records from ``first`` up to the owner record.

    Args:
        conn: Caller-owned connection with the indexed source rows.
        source: Indexed source row of the thread.
        assistant: Cited assistant record, checked at its stored bytes.
        owner: Cited owner record, checked at its stored bytes.
        first: The first line to return.
        budget: The operation read allowance charged for the read.

    Returns:
        Each line number and its raw record, in order.

    Raises:
        ValueError: If the bytes do not hold exactly those records.
        EvidenceLimitError: If the read allowance is exhausted.
    """
    start, end = assistant.original_locator, owner.original_locator
    if not (
        isinstance(start, JsonlOriginal)
        and isinstance(end, JsonlOriginal)
        and start.path == end.path
    ):
        return [
            (number, raw)
            for number, raw, _ in originals(
                conn,
                source,
                first,
                owner.line - 1,
                MAX_SCAN_BYTES,
                budget=budget,
            )
        ]
    # The commit has checked both records at their stored offsets, so only
    # the bytes between them are read, never the transcript from line 1.
    offset = start.byte_offset
    if first > assistant.line:
        offset += start.byte_length
    if end.byte_offset <= offset:
        raise ValueError("original_unavailable")
    span = JsonlOriginal(
        kind="jsonl",
        path=start.path,
        byte_offset=offset,
        byte_length=end.byte_offset - offset,
    )
    with ExitStack() as stack:
        raw = reread(stack, {}, span, span.byte_length, budget=budget)
    lines = raw.splitlines(keepends=True)
    if len(lines) != owner.line - first:
        raise ValueError("original_unavailable")
    for _ in lines[1:]:
        budget.consume_record()
    return list(enumerate(lines, first))


def _thread(record: EvidenceRecord) -> SourceRef:
    """Return the thread reference of a record."""
    return SourceRef(provider=record.provider, thread_id=record.thread_id)
