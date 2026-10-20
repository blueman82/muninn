"""Validate the one inventoried legacy installation without executing it."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any, cast

from install import snapshot_io
from install.constants import GIT_ENV
from install.context import Ctx, StepFailedError, link_text, must
from install.provider_paths import render_pinned
from install.record import Record
from install.transforms import ours
from muninn import platform_io, store_fingerprint
from muninn.obs_linux_service import require_interpreter, unit_matches

BASELINE = "5c1a69b0edff81df80836d72a80971a91a8f9520"


def owner_uid() -> int:
    """Require POSIX ownership checks for the inventoried legacy release."""
    if sys.platform == "win32":
        raise StepFailedError("legacy_unsupported_platform")
    return os.getuid()


def inventory() -> dict[str, Any]:
    """Read the committed exact-byte inventory as data only."""
    return json.loads(Path(__file__).with_suffix(".json").read_bytes())


def directory(path: Path, *, private: bool = False) -> list[int]:
    """Bind an ordinary owner directory without changing permissions."""
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or (
            sys.platform == "win32"
            and private
            and not platform_io.is_private(path, directory=True)
        )
        or (
            sys.platform != "win32"
            and (
                info.st_uid != owner_uid()
                or info.st_mode & (0o077 if private else 0o022)
            )
        )
        or path.resolve(strict=True) != path
    ):
        raise StepFailedError("legacy_directory_unsafe")
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]


def file_state(path: Path, *, private: bool = False) -> dict[str, Any]:
    """Stream a nofollow file and retain identity, complete bytes and mode."""
    expected = path.lstat()
    if (
        not stat.S_ISREG(expected.st_mode)
        or (
            sys.platform == "win32"
            and private
            and not platform_io.is_private(path)
        )
        or (
            sys.platform != "win32"
            and (
                expected.st_uid not in (0, owner_uid())
                or expected.st_mode & (0o077 if private else 0o022)
            )
        )
    ):
        raise StepFailedError("legacy_file_unsafe")
    digest = hashlib.sha256()
    with platform_io.open_regular(path, expected=expected) as handle:
        opened = os.fstat(handle.fileno())
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
    )
    current = path.lstat()
    stamp = [
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
        expected.st_size,
        expected.st_mtime_ns,
    ]
    # Windows path and descriptor views disagree on executable bits and ctime.
    if (
        any(
            [getattr(value, key) for key in fields]
            != [getattr(expected, key) for key in fields]
            for value in (opened, after, current)
        )
        or expected.st_mode != current.st_mode
        or opened.st_mode != after.st_mode
        or opened.st_ctime_ns != after.st_ctime_ns
        or expected.st_ctime_ns != current.st_ctime_ns
        or (
            sys.platform != "win32"
            and (
                expected.st_mode != opened.st_mode
                or expected.st_ctime_ns != opened.st_ctime_ns
            )
        )
    ):
        raise StepFailedError("legacy_file_changed")
    return {
        "stamp": [*stamp, expected.st_ctime_ns],
        "sha256": digest.hexdigest(),
    }


def tree_state(
    root: Path, *, private: bool = True, links: bool = False
) -> dict[str, Any]:
    """Reject links and bind every retained runtime file and directory."""
    directories = {".": directory(root, private=private)}
    files: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if links and path.is_symlink():
            info = path.lstat()
            files[name] = {
                "link": str(path.readlink()),
                "identity": [info.st_dev, info.st_ino],
                "target": (
                    directory(path.resolve(strict=True))
                    if path.is_dir()
                    else file_state(path.resolve(strict=True))
                ),
            }
        elif path.is_dir() and not path.is_symlink():
            directories[name] = directory(path)
        else:
            files[name] = file_state(path)
    return {"directories": directories, "files": files}


def expected_release(ctx: Ctx, root: Path) -> dict[str, Any]:
    """Require every installed file to equal its pinned rendered preimage."""
    actual = tree_state(root)
    expected = inventory()["files"]
    if set(actual["files"]) != set(expected):
        raise StepFailedError("legacy_runtime_mismatch")
    for name, wanted in expected.items():
        digest = wanted["sha256"]
        if "template" in wanted:
            digest = hashlib.sha256(
                render_pinned(ctx, name, wanted["template"].encode())
            ).hexdigest()
        found = actual["files"][name]
        if found["sha256"] != digest or bool(
            found["stamp"][2] & 0o111
        ) != bool(wanted["mode"] & 0o111):
            raise StepFailedError("legacy_runtime_mismatch")
    return actual


def source_state(ctx: Ctx) -> dict[str, Any]:
    """Inspect the settled legacy store under the caller's physical writer."""
    before = store_fingerprint.capture(ctx.data)
    path = ctx.data / "muninn.sqlite"
    with (
        snapshot_io.validated(path) as revalidate,
        closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn,
    ):
        revalidate()
        if (
            conn.execute("PRAGMA user_version").fetchone()[0] != 3
            or conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
            or [
                list(row)
                for row in conn.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "ORDER BY type,name"
                )
            ]
            != inventory()["sqlite_master"]
        ):
            raise StepFailedError("cutover_source_mismatch")
    store_fingerprint.verify(ctx.data, before)
    return json.loads(json.dumps(dataclasses.asdict(before)))


def links(ctx: Ctx) -> dict[str, Any]:
    """Bind exactly the managed selection, interpreter and stable launcher."""
    expected = {
        "current": BASELINE,
        "launcher": str(ctx.lib / "current/bin/muninn"),
    }
    paths = {
        "current": ctx.lib / "current",
        "launcher": ctx.muninn,
        "python": ctx.lib / "python",
    }
    found: dict[str, Any] = {}
    for name, path in paths.items():
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid():
            raise StepFailedError("legacy_selection_mismatch")
        text = link_text(path)
        if name in expected and text != expected[name]:
            raise StepFailedError("legacy_selection_mismatch")
        found[name] = {"text": text, "identity": [info.st_dev, info.st_ino]}
    python = Path(found["python"]["text"])
    if not python.is_absolute():
        raise StepFailedError("legacy_interpreter_mismatch")
    require_interpreter(python)
    found["interpreter"] = {
        "path": str(python.resolve(strict=True)),
        "file": file_state(python.resolve(strict=True)),
    }
    return found


def read_bytes(path: Path) -> bytes:
    """Read a bounded ordinary file between exact identity observations."""
    before = file_state(path)
    with platform_io.open_regular(path) as handle:
        data = handle.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024 or file_state(path) != before:
        raise StepFailedError("legacy_file_changed")
    return data


def _codex(ctx: Ctx, data: bytes) -> None:
    """Require the exact local marketplace and enabled managed plugin pair."""
    doc = tomllib.loads(data.decode())
    marketplace = doc.get("marketplaces", {}).get("muninn-local")
    plugin = doc.get("plugins", {}).get("muninn@muninn-local")
    if (marketplace is None) != (plugin is None):
        raise StepFailedError("legacy_provider_mismatch")
    if marketplace is not None and (
        marketplace.get("source_type") != "local"
        or marketplace.get("source")
        != str(ctx.lib / "current/integrations/codex")
        or plugin.get("enabled") is not True
    ):
        raise StepFailedError("legacy_provider_mismatch")


def _hooks(root: Path, name: str, data: bytes) -> None:
    """Require all configured managed groups to equal the inventoried set."""
    doc: dict[str, Any] = json.loads(data)
    relative = "settings-hooks.json" if name == "claude" else "hooks.json"
    wanted = json.loads(read_bytes(root / f"integrations/{name}/{relative}"))[
        "hooks"
    ]
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        raise StepFailedError("legacy_provider_mismatch")
    actual: dict[str, Any] = {}
    for event, handlers in cast(dict[str, Any], hooks).items():
        if not isinstance(handlers, list):
            raise StepFailedError("legacy_provider_mismatch")
        for group in cast(list[Any], handlers):
            if not isinstance(group, dict):
                raise StepFailedError("legacy_provider_mismatch")
            typed = cast(dict[str, Any], group)
            managed = (
                ours(typed)
                if name == "claude"
                else "muninn" in typed.get("command", "")
            )
            if managed:
                actual.setdefault(event, []).append(typed)
    if actual and actual != wanted:
        raise StepFailedError("legacy_provider_mismatch")


def provider_state(ctx: Ctx, root: Path) -> dict[str, Any]:
    """Bind finite owned provider entrypoints and untouched config bytes."""
    result: dict[str, Any] = {}
    for name, path in (
        ("claude", ctx.settings),
        ("codex", ctx.config),
        ("cursor", ctx.cursor_settings),
    ):
        if not os.path.lexists(path):
            result[name] = None
            continue
        before = file_state(path)
        data = read_bytes(path)
        if name == "codex":
            _codex(ctx, data)
        else:
            _hooks(root, name, data)
        if file_state(path) != before:
            raise StepFailedError("legacy_provider_changed")
        result[name] = before
    result["cache"] = _cache(ctx, root)
    return result


def _cache(ctx: Ctx, root: Path) -> dict[str, Any] | None:
    """Require an exact old plugin cache, if one was materialized."""
    if not os.path.lexists(ctx.cache):
        return None
    directory(ctx.cache)
    versions = list(ctx.cache.iterdir())
    if len(versions) != 1 or versions[0].name != "0.2.0":
        raise StepFailedError("legacy_provider_mismatch")
    cached = tree_state(versions[0], private=False)
    expected = tree_state(root / "integrations/codex", private=False)
    if {k: v["sha256"] for k, v in cached["files"].items()} != {
        k: v["sha256"] for k, v in expected["files"].items()
    }:
        raise StepFailedError("legacy_provider_mismatch")
    return cached


def candidate(ctx: Ctx, rec: Record) -> dict[str, Any]:
    """Verify prepared source against its exact archive before binding it."""
    actual = tree_state(ctx.lib / rec["sha"], links=True)
    raw = must(
        ctx,
        ["git", "-C", rec["repo"], "archive", "--format=tar", rec["sha"]],
        env=GIT_ENV,
    ).stdout
    expected: dict[str, tuple[str, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive.getmembers():
            if entry.isdir():
                continue
            if not entry.isfile():
                raise StepFailedError("legacy_candidate_unsafe")
            handle = archive.extractfile(entry)
            if handle is None:
                raise StepFailedError("legacy_candidate_unsafe")
            with handle:
                data = handle.read()
            if entry.name.startswith(("integrations/", "launchd/")):
                data = render_pinned(ctx, entry.name, data)
            expected[entry.name] = (
                hashlib.sha256(data).hexdigest(),
                bool(entry.mode & 0o111),
            )
    found = {
        name: (
            value.get("sha256"),
            bool(value.get("stamp", [0, 0, 0])[2] & 0o111),
        )
        for name, value in actual["files"].items()
        if not name.startswith(".venv/")
    }
    if found != expected:
        raise StepFailedError("legacy_candidate_changed")
    return actual


def installation(ctx: Ctx, rec: Record) -> dict[str, Any]:
    """Validate installed old bytes and identities before any disruption."""
    root = ctx.lib / BASELINE
    selection = links(ctx)
    runtime = expected_release(ctx, root)
    service = file_state(ctx.plist)
    if ctx.platform == "darwin":
        if read_bytes(ctx.plist) != read_bytes(
            root / "launchd/com.muninn.plist"
        ):
            raise StepFailedError("legacy_service_mismatch")
    elif ctx.platform == "linux":
        if not unit_matches(
            read_bytes(ctx.plist).decode(),
            Path(selection["python"]["text"]),
            ctx.lib / "current",
            ctx.data,
            {
                "HOME": str(ctx.home),
                "MUNINN_HOME": str(ctx.data),
                "CODEX_HOME": str(ctx.codex_home),
                "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
            },
        ):
            raise StepFailedError("legacy_service_mismatch")
    else:
        raise StepFailedError("legacy_boot_unsupported")
    return {
        "home": directory(ctx.home),
        "library": directory(ctx.lib, private=True),
        "repo": directory(Path(rec["repo"])),
        "runtime": runtime,
        "links": selection,
        "service": service,
        "providers": provider_state(ctx, root),
    }
