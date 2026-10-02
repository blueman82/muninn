"""Section-scoped edits of a TOML file found by a line-oriented scan.

We never parse the whole file: ``~/.codex/config.toml`` holds a credential
and arbitrary user settings, and a full parse-and-rewrite would reformat or
drop what we do not understand. Instead the file is cut into whole tables at
header lines, and only our own named tables are read or replaced. A layout we
do not recognise raises ``RefusedError`` and nothing is written. Error
messages carry line numbers and our own headers only, never file content.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Collection, Mapping, Sequence
from typing import Any, NamedTuple, cast

from install.errors import RefusedError

# Each alternative starts with a different character and the string bodies
# consume either one non-quote character or one escape pair, so none of these
# patterns can backtrack exponentially on hostile input.
_KEY = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
_HEADER = re.compile(
    rf"\s*\[\[?\s*{_KEY}(?:\s*\.\s*{_KEY})*\s*\]\]?\s*(?:#.*)?"
)
_VALUE = r"""("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|true|false)"""
_BODY = re.compile(rf"\s*([A-Za-z0-9_-]+)\s*=\s*{_VALUE}\s*")


class Block(NamedTuple):
    """One TOML table cut out of the file by line number.

    Attributes:
        header: The stripped header line, such as ``[plugins."x"]``.
        start: First line owned by the block (leading blank lines included).
        header_line: Line index of the header itself.
        end: One past the last line owned by the block.
    """

    header: str
    start: int
    header_line: int
    end: int


def _find_close(line: str, quote: str, start: int) -> int:
    """Find the closing quote token, honouring basic-string escapes.

    Args:
        line: The text being scanned.
        quote: The opening token, one quote character or a triple quote.
        start: Index to start searching from.

    Returns:
        The index of the closing token, or -1 when it is not on this line.
    """
    j = start
    while True:
        k = line.find(quote, j)
        # Literal strings (single quotes) have no escapes at all.
        if k < 0 or quote.startswith("'"):
            return k
        # A quote preceded by an odd run of backslashes is escaped.
        slashes = len(line[:k]) - len(line[:k].rstrip("\\"))
        if slashes % 2 == 0:
            return k
        j = k + 1


def _scan(line: str, ml: str | None) -> str | None:
    """Track whether a multi-line string is still open after a line.

    Header-looking text inside a multi-line string is data, not a table, so
    the block scan must know when it is inside one.

    Args:
        line: One line of the file.
        ml: The multi-line opener (triple quote) open at the start of the
            line, or None.

    Returns:
        The triple-quote token still open at the end of ``line``, or None.
    """
    j = 0
    while j < len(line):
        if ml:
            k = _find_close(line, ml, j)
            if k < 0:
                return ml
            ml, j = None, k + 3
            continue
        if line[j] == "#":
            return None
        if line.startswith(('"""', "'''"), j):
            ml, j = line[j : j + 3], j + 3
            continue
        if line[j] in "\"'":
            k = _find_close(line, line[j], j + 1)
            if k < 0:
                # Invalid TOML; stay conservative and treat the rest of the
                # line as plain text so the header scan keeps working.
                return None
            j = k + 1
            continue
        j += 1
    return None


def _blocks(text: str) -> tuple[list[str], list[Block]]:
    """Cut a file into its tables.

    A block owns the blank lines right above its header, so removing or
    re-inserting a block never changes its neighbours' bytes.

    Args:
        text: The whole TOML file.

    Returns:
        The lines (with line endings) and one ``Block`` per table.
    """
    lines = text.splitlines(keepends=True)
    heads: list[int] = []
    ml: str | None = None
    for i, line in enumerate(lines):
        if ml is None and _HEADER.fullmatch(line.rstrip("\r\n")):
            heads.append(i)
            continue
        ml = _scan(line, ml)
    starts: list[tuple[str, int, int]] = []
    floor = 0
    for h in heads:
        s = h
        while s > floor and not lines[s - 1].strip():
            s -= 1
        starts.append((lines[h].strip(), s, h))
        floor = h + 1
    ends = [s for _, s, _ in starts[1:]] + [len(lines)]
    # ``ends`` has one entry too many when the file has no tables at all, so
    # the lengths legitimately differ; the shorter side (empty) wins.
    return lines, [
        Block(header, s, h, e)
        for (header, s, h), e in zip(starts, ends, strict=False)
    ]


def _only(blocks: Sequence[Block], header: str) -> Block | None:
    """Return the single block with ``header``.

    Args:
        blocks: Every table in the file.
        header: The header to look for.

    Returns:
        The block, or None when the file has no such table.

    Raises:
        RefusedError: If the header appears more than once; TOML forbids it
            and we cannot tell which copy to edit.
    """
    found = [b for b in blocks if b.header == header]
    if len(found) > 1:
        raise RefusedError(f"{header} appears {len(found)} times")
    return found[0] if found else None


def get_section(text: str, header: str) -> str | None:
    """Return the raw block text (with its leading blank lines).

    Args:
        text: The whole TOML file.
        header: Header of the table to read.

    Returns:
        The block text, or None when the table is absent.
    """
    lines, blocks = _blocks(text)
    b = _only(blocks, header)
    return None if b is None else "".join(lines[b.start : b.end])


def index_of(text: str, header: str) -> int | None:
    """Find the position of a table among all tables in the file.

    Args:
        text: The whole TOML file.
        header: Header of the table to locate.

    Returns:
        A table index, or None when the table is absent.
    """
    _, blocks = _blocks(text)
    names = [b.header for b in blocks]
    return names.index(header) if header in names else None


def put_section(
    text: str, header: str, block: str | None, at: int | None = None
) -> str:
    """Replace ``header``'s block with ``block``.

    A new block is inserted before the table now at index ``at``, or at the
    end of the file when ``at`` is None or past the last table.

    Args:
        text: The whole TOML file.
        header: Header of the table to replace.
        block: Replacement text, or None to remove the table.
        at: Table index to insert a new block before.

    Returns:
        The edited file text.

    Raises:
        RefusedError: If an append is needed and the file does not end with
            a newline, since the new block would fuse with the last line.
    """
    lines, blocks = _blocks(text)
    b = _only(blocks, header)
    if b is not None:
        lines[b.start : b.end] = [block] if block else []
        return "".join(lines)
    if not block:
        return text
    if at is not None and at < len(blocks):
        pos = blocks[at].start
    else:
        if lines and not lines[-1].endswith("\n"):
            raise RefusedError("file does not end with a newline")
        pos = len(lines)
    if pos < len(lines) and not block.endswith("\n"):
        block += "\n"
    lines[pos:pos] = [block]
    return "".join(lines)


def parse_section(
    text: str, header: str, keys: Collection[str]
) -> dict[str, Any] | None:
    """Read the values of one named block, refusing anything but simple lines.

    Args:
        text: The whole TOML file.
        header: Header of the table to read.
        keys: The only key names the table may contain.

    Returns:
        The table's values, or None when the table is absent.

    Raises:
        RefusedError: If a line is not a plain ``key = scalar`` pair, names
            an unexpected key, or repeats a key.
    """
    raw = get_section(text, header)
    if raw is None:
        return None
    body = raw.splitlines()
    first = next(i for i, line in enumerate(body) if line.strip())
    seen: set[str] = set()
    for n, line in enumerate(body[first + 1 :], first + 2):
        if not line.strip():
            continue
        m = _BODY.fullmatch(line)
        if not m or m.group(1) not in keys or m.group(1) in seen:
            raise RefusedError(f"{header}: unexpected line {n} of the section")
        seen.add(m.group(1))
    table: dict[str, Any] = tomllib.loads(raw)
    # A dotted header nests the values; unwrap to the innermost table.
    while len(table) == 1:
        (only,) = table.values()
        if not isinstance(only, dict):
            break
        table = cast(dict[str, Any], only)
    return table


def _check_markers(
    lines: Sequence[str],
    blocks: Sequence[Block],
    headers: Collection[str],
    markers: Sequence[str],
) -> None:
    """Refuse our identifiers appearing anywhere but our own sections.

    Args:
        lines: Every line of the file.
        blocks: Every table in the file.
        headers: Headers of the sections we own.
        markers: Substrings that identify us.

    Raises:
        RefusedError: If a marker occurs on a line other than the header of
            one of our sections, where we could not edit it safely.
    """
    ours = {b.header_line for b in blocks if b.header in headers}
    for i, line in enumerate(lines):
        if i not in ours and any(m in line for m in markers):
            raise RefusedError(
                f"line {i + 1}: our identifier outside our sections"
            )


def scan_named(
    text: str, keys: Mapping[str, Collection[str]], markers: Sequence[str]
) -> dict[str, dict[str, Any] | None]:
    """Parse our named sections and refuse any other use of our markers.

    Lines outside our sections are only searched for the marker strings,
    never parsed.

    Args:
        text: The whole TOML file.
        keys: Allowed key names per section header.
        markers: Substrings that identify us.

    Returns:
        Values per header, None for a section that is absent.
    """
    lines, blocks = _blocks(text)
    _check_markers(lines, blocks, keys, markers)
    return {h: parse_section(text, h, k) for h, k in keys.items()}


def toml_check(
    before: str,
    after: str,
    headers: Collection[str],
    markers: Sequence[str],
) -> None:
    """Prove that only the named sections changed: the rest is identical.

    Args:
        before: File text before the edit.
        after: File text after the edit.
        headers: Headers of the sections we are allowed to change.
        markers: Substrings that identify us.

    Raises:
        RefusedError: If any byte outside the named sections differs.
    """
    keep: list[str] = []
    for text in (before, after):
        lines, blocks = _blocks(text)
        _check_markers(lines, blocks, headers, markers)
        # Delete back to front so earlier line numbers stay valid.
        for b in reversed(blocks):
            if b.header in headers:
                del lines[b.start : b.end]
        keep.append("".join(lines))
    if keep[0] != keep[1]:
        raise RefusedError("bytes outside the named sections changed")
