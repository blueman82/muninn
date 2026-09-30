"""Race-safe, key-scoped edits of provider config files (JSON and TOML).

JSON is edited by key path and re-serialised in the file's own indent.
TOML is edited only as whole named sections found by a line-oriented
scan; any layout we do not recognise raises Refused and nothing is
written. Other TOML keys are never parsed. Error messages carry line
numbers and our own section headers only, never file content:
~/.codex/config.toml holds a credential.
"""

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path


class Refused(Exception):
    """Unexpected layout or unproven edit; nothing was written."""


class Raced(Exception):
    """The file kept changing under us; our edit was not applied."""


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(path: Path, data: bytes, mode: int) -> Path:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the same dir, fsync, rename over `path`, fsync dir."""
    tmp = _write_temp(path, data, mode)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def edit_file(path: Path, transform, check, retries: int = 3):
    """Re-read, transform, prove, then replace atomically (A7).

    transform(bytes) -> bytes; check(before, after) raises Refused. If
    the file changes between our read and our replace, the edit is
    recomputed from the new content (re-merge). The written file is
    re-read and re-checked. Returns (before, after).
    """
    for _ in range(retries):
        before = _read(path)
        after = transform(before)
        check(before, after)
        if after == before:
            return before, after
        tmp = _write_temp(path, after, path.stat().st_mode & 0o777)
        try:
            # ponytail: compare-then-rename leaves a sub-millisecond window;
            # the providers take no lock we could share.
            if _read(path) != before:
                continue
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        on_disk = _read(path)
        if on_disk != after:
            raise Raced(f"{path.name} changed right after our write")
        check(before, on_disk)
        return before, after
    raise Raced(f"{path.name} kept changing; gave up after {retries} tries")


# ---- JSON -------------------------------------------------------------


def load_json(data: bytes) -> dict:
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise Refused("top level is not a JSON object")
    return obj


def dump_like(original: bytes, obj) -> bytes:
    """Serialise in the original's indent, keeping its final newline."""
    m = re.search(rb"\n( +)\S", original)
    text = json.dumps(obj, indent=len(m.group(1)) if m else 2)
    return (text + ("\n" if original.endswith(b"\n") else "")).encode()


def _parent(obj, path, create):
    for key in path[:-1]:
        if key not in obj and create:
            obj[key] = {}
        obj = obj.get(key)
        if not isinstance(obj, dict):
            if obj is None and not create:
                return None
            raise Refused(f"{'.'.join(path)}: parent is not an object")
    return obj


def jget(obj, path):
    """(present, value, index-in-parent) for a key path."""
    parent = _parent(obj, path, create=False)
    if parent is None or path[-1] not in parent:
        return False, None, None
    return True, parent[path[-1]], list(parent).index(path[-1])


def jset(obj, path, value, index=None) -> None:
    """Set in place; a new key goes at `index` (default: last)."""
    parent = _parent(obj, path, create=True)
    if path[-1] in parent or index is None:
        parent[path[-1]] = value
        return
    items = list(parent.items())
    items.insert(index, (path[-1], value))
    parent.clear()
    parent.update(items)


def jdel(obj, path) -> None:
    parent = _parent(obj, path, create=False)
    if parent is not None:
        parent.pop(path[-1], None)


def json_check(before: bytes, after: bytes, paths) -> None:
    """Prove that nothing outside `paths` changed (A7)."""
    masked = []
    for data in (before, after):
        obj = load_json(data)
        for path in paths:
            jset(obj, path, "\0touched")
        masked.append(obj)
    if masked[0] != masked[1]:
        raise Refused("keys outside the edited paths changed")


# ---- TOML -------------------------------------------------------------

_KEY = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
_HEADER = re.compile(
    rf"\s*\[\[?\s*{_KEY}(?:\s*\.\s*{_KEY})*\s*\]\]?\s*(?:#.*)?"
)
_VALUE = r"""("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|true|false)"""
_BODY = re.compile(rf"\s*([A-Za-z0-9_-]+)\s*=\s*{_VALUE}\s*")


def _find_close(line: str, quote: str, start: int) -> int:
    """Index of the closing `quote` token, honouring basic-string escapes."""
    j = start
    while True:
        k = line.find(quote, j)
        if k < 0 or quote.startswith("'"):
            return k
        slashes = len(line[:k]) - len(line[:k].rstrip("\\"))
        if slashes % 2 == 0:
            return k
        j = k + 1


def _scan(line: str, ml):
    """Return the multi-line string still open at the end of `line`."""
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
                return None  # invalid TOML; the header scan stays conservative
            j = k + 1
            continue
        j += 1
    return None


def _blocks(text: str):
    """(lines, [(header, start, header_line, end)]) for every table.

    A block owns the blank lines right above its header, so removing or
    re-inserting a block never changes its neighbours' bytes.
    """
    lines = text.splitlines(keepends=True)
    heads, ml = [], None
    for i, line in enumerate(lines):
        if ml is None and _HEADER.fullmatch(line.rstrip("\r\n")):
            heads.append(i)
            continue
        ml = _scan(line, ml)
    blocks, floor = [], 0
    for h in heads:
        s = h
        while s > floor and not lines[s - 1].strip():
            s -= 1
        blocks.append([lines[h].strip(), s, h])
        floor = h + 1
    ends = [b[1] for b in blocks[1:]] + [len(lines)]
    return lines, [(*b, e) for b, e in zip(blocks, ends)]


def _only(blocks, header):
    found = [b for b in blocks if b[0] == header]
    if len(found) > 1:
        raise Refused(f"{header} appears {len(found)} times")
    return found[0] if found else None


def get_section(text: str, header: str):
    """Raw block text (with its leading blank lines), or None."""
    lines, blocks = _blocks(text)
    b = _only(blocks, header)
    return None if b is None else "".join(lines[b[1] : b[3]])


def anchor_of(text: str, header: str):
    """Header of the block before `header` ('' if first; None if absent)."""
    _, blocks = _blocks(text)
    for i, b in enumerate(blocks):
        if b[0] == header:
            return blocks[i - 1][0] if i else ""
    return None


def put_section(text: str, header: str, block, after=None) -> str:
    """Replace `header`'s block with `block` (None removes it).

    A new block goes after the block headed `after` ('' = before the
    first table); an unknown or repeated anchor means end of file.
    """
    lines, blocks = _blocks(text)
    b = _only(blocks, header)
    if b is not None:
        lines[b[1] : b[3]] = [block] if block else []
        return "".join(lines)
    if not block:
        return text
    anchors = [x for x in blocks if x[0] == after]
    if after == "" and blocks:
        pos = blocks[0][1]
    elif len(anchors) == 1:
        pos = anchors[0][3]
    else:
        if lines and not lines[-1].endswith("\n"):
            raise Refused("file does not end with a newline")
        pos = len(lines)
    if pos < len(lines) and not block.endswith("\n"):
        block += "\n"
    lines[pos:pos] = [block]
    return "".join(lines)


def parse_section(text: str, header: str, keys):
    """Values of one named block, refusing anything but simple lines."""
    raw = get_section(text, header)
    if raw is None:
        return None
    body = raw.splitlines()
    first = next(i for i, line in enumerate(body) if line.strip())
    seen = set()
    for n, line in enumerate(body[first + 1 :], first + 2):
        if not line.strip():
            continue
        m = _BODY.fullmatch(line)
        if not m or m.group(1) not in keys or m.group(1) in seen:
            raise Refused(f"{header}: unexpected line {n} of the section")
        seen.add(m.group(1))
    table = tomllib.loads(raw)
    while len(table) == 1 and isinstance(next(iter(table.values())), dict):
        table = next(iter(table.values()))
    return table


def _check_markers(lines, blocks, headers, markers) -> None:
    ours = {b[2] for b in blocks if b[0] in headers}
    for i, line in enumerate(lines):
        if i not in ours and any(m in line for m in markers):
            raise Refused(f"line {i + 1}: our identifier outside our sections")


def scan_named(text: str, keys: dict, markers) -> dict:
    """Parse our named sections; refuse any other use of our markers.

    Returns {header: values or None}. Lines outside our sections are
    only searched for the marker strings, never parsed.
    """
    lines, blocks = _blocks(text)
    _check_markers(lines, blocks, keys, markers)
    return {h: parse_section(text, h, k) for h, k in keys.items()}


def toml_check(before: str, after: str, headers, markers) -> None:
    """Prove that only the named sections changed: the rest is identical."""
    keep = []
    for text in (before, after):
        lines, blocks = _blocks(text)
        _check_markers(lines, blocks, headers, markers)
        for b in reversed(blocks):
            if b[0] in headers:
                del lines[b[1] : b[3]]
        keep.append("".join(lines))
    if keep[0] != keep[1]:
        raise Refused("bytes outside the named sections changed")
