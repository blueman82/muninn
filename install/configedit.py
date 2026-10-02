"""Race-safe, key-scoped edits of provider config files (JSON and TOML).

JSON is edited by key path and re-serialised in the file's own indent. TOML
is edited only as whole named sections (see ``install.tomledit``); other TOML
keys are never parsed. Error messages carry line numbers and our own section
headers only, never file content: ~/.codex/config.toml holds a credential.

The TOML helpers and both error types are re-exported here so callers have
one place to import from.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from install.errors import RacedError, RefusedError
from install.tomledit import (
    get_section,
    index_of,
    parse_section,
    put_section,
    scan_named,
    toml_check,
)

__all__ = [
    "JsonPath",
    "RacedError",
    "RefusedError",
    "atomic_write",
    "dump_like",
    "edit_file",
    "get_section",
    "index_of",
    "jdel",
    "jget",
    "jset",
    "json_check",
    "load_json",
    "parse_section",
    "put_section",
    "scan_named",
    "toml_check",
]

JsonPath = tuple[str, ...]


def _read(path: Path) -> bytes:
    """Read a file; a seam so tests can simulate a concurrent writer."""
    return path.read_bytes()


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry so a completed rename survives a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(path: Path, data: bytes, mode: int) -> Path:
    """Write ``data`` to a fsynced temp file next to ``path``.

    The temp file lives in the same directory so the later rename stays on
    one filesystem and is therefore atomic.

    Args:
        path: The file the data is destined for.
        data: Bytes to write.
        mode: Permission bits, applied before any data is written so the
            content is never readable under a wider mode.

    Returns:
        The temp file's path.
    """
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
    """Write a file atomically: temp, fsync, rename over ``path``, fsync dir.

    Args:
        path: Destination file.
        data: Bytes to write.
        mode: Permission bits for the new file.
    """
    tmp = _write_temp(path, data, mode)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def edit_file(
    path: Path,
    transform: Callable[[bytes], bytes],
    check: Callable[[bytes, bytes], None],
    retries: int = 3,
) -> tuple[bytes, bytes]:
    """Re-read, transform, prove, then replace a file atomically.

    If the file changes between our read and our replace, the edit is
    recomputed from the new content. The written file is re-read and
    re-checked.

    Args:
        path: The file to edit; it must exist.
        transform: Maps the current bytes to the edited bytes.
        check: Called as ``check(before, after)``; raises ``RefusedError``
            when the edit touches more than it should.
        retries: How many times to recompute after losing a race.

    Returns:
        The bytes before and after the edit.

    Raises:
        RacedError: If the file kept changing, or changed right after our
            write.
    """
    for _ in range(retries):
        before = _read(path)
        after = transform(before)
        check(before, after)
        if after == before:
            return before, after
        # Keep the file's own permissions; the providers may use 0600.
        tmp = _write_temp(path, after, path.stat().st_mode & 0o777)
        try:
            # ponytail: compare-then-rename leaves a sub-millisecond window;
            # the providers take no lock we could share.
            if _read(path) != before:
                continue
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        on_disk = _read(path)
        if on_disk != after:
            raise RacedError(f"{path.name} changed right after our write")
        check(before, on_disk)
        return before, after
    raise RacedError(
        f"{path.name} kept changing; gave up after {retries} tries"
    )


def load_json(data: bytes) -> dict[str, Any]:
    """Parse a JSON document that must be an object.

    Args:
        data: The file bytes.

    Returns:
        The parsed object.

    Raises:
        RefusedError: If the top level is not an object.
    """
    obj: Any = json.loads(data)
    if not isinstance(obj, dict):
        raise RefusedError("top level is not a JSON object")
    # JSON object keys are always strings; isinstance cannot say so.
    return cast(dict[str, Any], obj)


def dump_like(original: bytes, obj: dict[str, Any]) -> bytes:
    """Serialise in the original's indent, keeping its final newline.

    Args:
        original: The file bytes the object was loaded from.
        obj: The edited object.

    Returns:
        The new file bytes.
    """
    m = re.search(rb"\n( +)\S", original)
    text = json.dumps(obj, indent=len(m.group(1)) if m else 2)
    return (text + ("\n" if original.endswith(b"\n") else "")).encode()


def _parent(
    obj: dict[str, Any], path: JsonPath, create: bool
) -> dict[str, Any] | None:
    """Walk to the object that holds the last key of ``path``.

    Args:
        obj: The document root.
        path: Key path whose parent is wanted.
        create: Create missing intermediate objects instead of stopping.

    Returns:
        The parent object, or None when it is absent and not created.

    Raises:
        RefusedError: If an intermediate value exists but is not an object.
    """
    node = obj
    for key in path[:-1]:
        if key not in node and create:
            node[key] = {}
        child: Any = node.get(key)
        if not isinstance(child, dict):
            if child is None and not create:
                return None
            raise RefusedError(f"{'.'.join(path)}: parent is not an object")
        node = cast(dict[str, Any], child)
    return node


def jget(obj: dict[str, Any], path: JsonPath) -> tuple[bool, Any, int | None]:
    """Look up a key path.

    Args:
        obj: The document root.
        path: Key path to read.

    Returns:
        ``(present, value, index-in-parent)``; the index lets a restore put
        the key back in its original position.
    """
    parent = _parent(obj, path, create=False)
    if parent is None or path[-1] not in parent:
        return False, None, None
    return True, parent[path[-1]], list(parent).index(path[-1])


def jset(
    obj: dict[str, Any],
    path: JsonPath,
    value: Any,
    index: int | None = None,
) -> None:
    """Set a key path in place; a new key goes at ``index`` (default last).

    Args:
        obj: The document root.
        path: Key path to write.
        value: The value to store.
        index: Position for a key that does not exist yet.
    """
    parent = _parent(obj, path, create=True)
    assert parent is not None  # create=True always yields an object
    if path[-1] in parent or index is None:
        parent[path[-1]] = value
        return
    # dicts cannot insert at a position, so rebuild in the wanted order.
    items = list(parent.items())
    items.insert(index, (path[-1], value))
    parent.clear()
    parent.update(items)


def jdel(obj: dict[str, Any], path: JsonPath) -> None:
    """Delete a key path in place; absent keys are ignored."""
    parent = _parent(obj, path, create=False)
    if parent is not None:
        parent.pop(path[-1], None)


def json_check(before: bytes, after: bytes, paths: Sequence[JsonPath]) -> None:
    """Prove that nothing outside ``paths`` changed.

    Args:
        before: File bytes before the edit.
        after: File bytes after the edit.
        paths: The key paths we are allowed to change.

    Raises:
        RefusedError: If the documents differ anywhere else.
    """
    masked: list[dict[str, Any]] = []
    for data in (before, after):
        obj = load_json(data)
        for path in paths:
            # Overwrite our keys with one sentinel so only foreign changes
            # make the two documents differ.
            jset(obj, path, "\0touched")
        masked.append(obj)
    if masked[0] != masked[1]:
        raise RefusedError("keys outside the edited paths changed")
