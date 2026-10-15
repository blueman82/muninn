"""Hook output framing: turn store content into a bounded, safe text block.

Everything that comes from the store reaches the model through this module.
It is framed as untrusted data, redacted, stripped of anything that could
close the frame early, and cut to a character limit.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import TypedDict

from muninn import classify, query

BLOCK_LIMIT = 4000  # SessionStart block, characters
RECALL_LIMIT = 1500  # prompt-time recall block, characters
ENTRY_TEXT = 300  # characters of one entry's text in a block
SNIPPET = 300  # characters of one recalled snippet

OPEN = '<muninn-memory source="muninn" trust="untrusted-data"'
CLOSE = "</muninn-memory>"
USAGE = (
    "Before answering about earlier work or re-deciding a recorded choice, run"
    ' `muninn search "<words>"` and open what you cite. Record an owner'
    " decision"
    " only if the owner said it: `muninn know add --kind decision --text …"
    ' --cite REF --quote "<verbatim>"` (REF from `muninn search`; check the'
    " quote with `muninn quote-check`; see `muninn know add --help`)."
    " For owner requests use search --kind prompt; for assistant reports"
    " use --kind reply. Widen if needed; verify authorship in the original."
)
OPEN_HINT = (
    "Open a hit with `muninn open <ref> --context 3`;"
    " browse with `muninn sessions`. Search page sizes vary: if has_more,"
    " repeat the search with --page N+1. See `muninn --help` for cursors. "
    + query.PREVIEW_NOTICE
)
RECALL_HINT = (
    "Previews: navigation only; use opened, cited eligible originals or"
    " say unknown.\n"
    "Open a hit with `muninn open <ref> --context 3`."
)

# The `<` of a frame delimiter, however spaced or cased. Only the `<` is
# matched (the rest is lookahead) so the substitution leaves the delimiter's
# own text readable.
_FRAME = re.compile(r"(?i)<(?=\s*/?\s*muninn-(?:memory|recall))")


class BlockEntry(TypedDict):
    """A knowledge entry pushed at session start."""

    id: str
    kind: str
    date: str
    actor: str
    text: str
    quote: str
    cite: str


class RecallEntry(TypedDict):
    """A knowledge entry matched by a prompt, with all its citations."""

    id: str
    kind: str
    date: str
    actor: str
    text: str
    cites: list[str]


class RecallHit(TypedDict):
    """An event matched by a prompt."""

    id: int
    provider: str
    role: str
    kind: str
    session: str
    ts: str | None
    ref: str
    snippet: str


def escape_delimiter(text: str) -> str:
    """Remove the ``<`` of every frame delimiter in the text.

    ``<muninn-memory`` and ``<muninn-recall``, opening or closing, in any case
    and with any inner spacing, lose their ``<`` so a frame closes once.

    Args:
        text: Untrusted text.

    Returns:
        The text with delimiters defused as ``&lt;``.
    """
    return _FRAME.sub("&lt;", text)


def clean(text: object, limit: int) -> str:
    """Make store text safe for a frame: one line, no secrets, bounded.

    Redaction runs first and the cut last, so the limit applies to the
    final text.

    Args:
        text: Value to render; converted with ``str``.
        limit: Maximum characters, including the trailing ellipsis.

    Returns:
        A single line of at most ``limit`` characters.
    """
    flat = " ".join(str(text).split())
    safe = escape_delimiter(classify.redact(flat)[0])
    return safe if len(safe) <= limit else safe[: limit - 1] + "…"


def frame(attrs: str, lines: list[str]) -> str:
    """Wrap lines in the untrusted-data frame.

    Args:
        attrs: Extra attributes for the opening tag, with a leading space.
        lines: Body lines; already cleaned by the caller.

    Returns:
        The framed block.
    """
    return "\n".join([f"{OPEN}{attrs}>", *lines, CLOSE])


def _entry_line(entry: BlockEntry) -> str:
    """Format one session-start entry with its quote and citation."""
    text = clean(entry["text"], ENTRY_TEXT)
    quote = clean(entry["quote"], 120)
    return (
        f"- {entry['id']} [{entry['kind']} {entry['date']}"
        f" by:{clean(entry['actor'], 40)}] {text}"
        f' (quote: "{quote}"; cite: {clean(entry["cite"], 120)})'
    )


def render_block(
    entries: Sequence[BlockEntry],
    scope_label: str,
    *,
    notes: tuple[str, ...] = (),
    limit: int = BLOCK_LIMIT,
) -> str:
    """Render the SessionStart block.

    The last entries are dropped until the block fits ``limit``; if even
    the empty block does not, it is cut and re-closed.

    Args:
        entries: Entries from ``knowledge.block_entries``, newest first.
        scope_label: Name of the repo or scope, shown in the heading.
        notes: Extra lines, such as a stale-index or failed-sources warning.
        limit: Maximum characters.

    Returns:
        The framed block.
    """
    label = clean(scope_label, 80)
    tail = [USAGE, OPEN_HINT, *(clean(n, 200) for n in notes)]
    block = ""
    for shown in range(len(entries), -1, -1):
        head: list[str] = []
        if shown:
            head = [
                f"Project knowledge for {label} ({shown} current; cited; may"
                " be stale — verify with `muninn know show K<id>`):",
                *(_entry_line(e) for e in entries[:shown]),
            ]
        block = frame("", head + tail)
        if len(block) <= limit:
            return block
    return block[: limit - len(CLOSE) - 1] + "\n" + CLOSE


def _knowledge_line(entry: RecallEntry, cap: int = ENTRY_TEXT) -> str:
    """Format one matched knowledge entry with its citations."""
    cites = ", ".join(clean(c, 120) for c in entry["cites"])
    return (
        f"- {entry['id']} [{entry['kind']} {entry['date']}"
        f" by:{clean(entry['actor'], 40)}]"
        f" {clean(entry['text'], min(cap, ENTRY_TEXT))}"
        + (f" (cites: {cites})" if cites else "")
    )


def _hit_line(hit: RecallHit, cap: int) -> str:
    """Format one matched event; ``cap`` bounds its snippet."""
    # The «» marks highlight the match for the CLI; they are noise here.
    plain = hit["snippet"].replace("«", "").replace("»", "")
    head = " · ".join(
        clean(part, 120)
        for part in (
            f"{hit['provider']} {hit['role']} {hit['kind']}",
            f"session {hit['session']}",
            hit["ts"] or "no ts",
            hit["ref"],
        )
    )
    return f"- [{head}] {clean(plain, cap)}"


def recall_text(
    entries: Sequence[RecallEntry],
    hits: Sequence[RecallHit],
    notes: Sequence[str],
    limit: int = RECALL_LIMIT,
    *,
    feedback: str = "",
    trace: dict[str, object] | None = None,
) -> str:
    """Render the prompt-time recall block within a limit.

    Knowledge comes first, then events. The last items are dropped, and
    then the snippets shortened, until the block fits.

    Args:
        entries: Matched knowledge entries.
        hits: Matched events.
        notes: Extra lines, such as a stale-index or failed-sources warning.
        limit: Maximum characters.
        feedback: Native delivery identifier and assessment instruction.
        trace: Receives the sources that survive rendering.

    Returns:
        The framed block, or an empty string when there are no items or
        not even one fits.
    """
    if trace is not None:
        trace.update(
            delivered_ids=[],
            delivered_knowledge_ids=[],
            delivered_sources=[],
            delivered_knowledge=[],
        )
    tail = [
        *(clean(n, 200) for n in notes),
        *([feedback] if feedback else []),
        RECALL_HINT,
    ]
    for cap in (SNIPPET, 200, 120):
        items = [_knowledge_line(e, cap) for e in entries]
        items += [_hit_line(h, cap) for h in hits]
        for count in range(len(items), 0, -1):
            lines = [classify.NOTICE, *items[:count], *tail]
            block = frame(' kind="recall"', lines)
            if len(block) <= limit:
                if trace is not None:
                    kept_entries = entries[:count]
                    kept_hits = hits[: max(0, count - len(entries))]
                    trace["delivered_ids"] = [h["id"] for h in kept_hits]
                    trace["delivered_knowledge_ids"] = [
                        int(e["id"][1:]) for e in kept_entries
                    ]
                    trace["delivered_sources"] = [
                        {"ref": h["ref"], "cap": cap} for h in kept_hits
                    ]
                    trace["delivered_knowledge"] = [
                        {
                            "id": e["id"],
                            "cites": e["cites"],
                            "cap": cap,
                            "sha256": hashlib.sha256(
                                e["text"].encode()
                            ).hexdigest(),
                        }
                        for e in kept_entries
                    ]
                return block
    # ponytail: a lone item over the cap gives no block; the longest
    # realistic one is about 470 characters against Codex's 900
    return ""
