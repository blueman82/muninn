"""Resolve the trusted local current Codex package within finite bounds."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, ValidationError

from muninn import review_codex_launch
from muninn.knowledge_models import StrictModel, Text
from muninn.platform_io import open_regular
from muninn.review_json import (
    canonical_json,
    canonical_json_value,
    unique_object,
)
from muninn.review_rounds import round_schema
from muninn.review_runtime_models import (
    FileStamp,
    ManagedCodexSelection,
    RunnerConfig,
    RuntimeIdentity,
)

FILE_LIMIT = 384 * 1024 * 1024
PACKAGE_LIMIT = 512 * 1024 * 1024
RECIPE_DIGEST = hashlib.sha256(
    b"codex-json-review-v1:version:help:exec-help"
).hexdigest()
VALIDATOR_DIGEST = hashlib.sha256(b"codex-managed-validator-v1").hexdigest()


class PackageLayout(StrictModel):
    """Require the supported complete standalone layout declaration."""

    layout_version: Literal[1] = Field(alias="layoutVersion")
    version: Text
    target: Literal["aarch64-apple-darwin"]
    variant: Literal["codex"]
    entrypoint: Literal["bin/codex"]
    exec_server: Literal["bin/codex-exec-server"] = Field(alias="execServer")
    resources_dir: Literal["codex-resources"] = Field(alias="resourcesDir")
    path_dir: Literal["codex-path"] = Field(alias="pathDir")


def manager_root() -> Path:
    """Return the local manager bound by the managed selection mode."""
    return Path.home() / ".codex" / "packages" / "standalone"


def stamp(path: Path) -> FileStamp:
    """Observe a path without following its final symbolic link."""
    return _stamp(path, path.lstat())


def _stamp(path: Path, info: os.stat_result) -> FileStamp:
    """Represent path and opened-file observations with the same fields."""
    return FileStamp(
        path=path,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        mode=info.st_mode,
        uid=info.st_uid,
        gid=info.st_gid,
        link_target=(
            str(path.readlink()) if stat.S_ISLNK(info.st_mode) else None
        ),
    )


def matches(identity: RuntimeIdentity) -> bool:
    """Recheck selector and complete file identities before each activation."""
    try:
        return identity.selector_stamp.path.resolve(
            strict=True
        ) == identity.package_root and all(
            stamp(item.path) == item
            for item in (
                identity.selector_stamp,
                *identity.file_stamps,
            )
        )
    except (OSError, RuntimeError):
        return False


def selection_changed(identity: RuntimeIdentity) -> bool:
    """Recognize selector replacement or a missing old native target only."""
    try:
        if stamp(identity.selector_stamp.path) != identity.selector_stamp:
            return True
        Path(identity.requested.executable).lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def same_execution(first: RuntimeIdentity, second: RuntimeIdentity) -> bool:
    """Compare actual program facts independently of file stamps."""
    return (
        first.requested,
        first.package_root,
        first.executable_sha256,
        first.package_sha256,
    ) == (
        second.requested,
        second.package_root,
        second.executable_sha256,
        second.package_sha256,
    )


def hash_file(path: Path, limit: int, deadline: float) -> tuple[str, int]:
    """Hash a bounded regular file while honoring the original deadline."""
    before = stamp(path)
    if not stat.S_ISREG(before.mode) or before.size > limit:
        raise ValueError("runtime_structure_limit")
    remaining = before.size
    hashed = hashlib.sha256()
    with open_regular(path) as checked:
        if _stamp(path, os.fstat(checked.fileno())) != before:
            raise ValueError("runtime_selection_unavailable")
        while remaining:
            if time.monotonic() >= deadline:
                raise ValueError("runtime_probe_unavailable")
            raw = os.read(checked.fileno(), min(65536, remaining))
            if not raw:
                raise ValueError("runtime_selection_unavailable")
            hashed.update(raw)
            remaining -= len(raw)
        if _stamp(path, os.fstat(checked.fileno())) != before:
            raise ValueError("runtime_selection_unavailable")
    if stamp(path) != before:
        raise ValueError("runtime_selection_unavailable")
    return hashed.hexdigest(), before.size


def _safe(path: Path, info: FileStamp) -> None:
    """Require owner/root storage without other-user replacement authority."""
    if sys.platform == "win32":
        raise ValueError("runtime_interface_unsupported")
    if (
        info.uid not in (os.getuid(), 0)
        or (not stat.S_ISLNK(info.mode) and info.mode & 0o022)
        or review_codex_launch.has_write_acl(path)
    ):
        raise ValueError("runtime_selection_unsafe")


def _root(selection: ManagedCodexSelection) -> tuple[Path, FileStamp]:
    """Confine the single manager selector to its canonical releases tree."""
    root = selection.manager_root
    if (
        root != manager_root()
        or not root.is_absolute()
        or root.resolve() != root
    ):
        raise ValueError("runtime_selection_unsafe")
    for path in (root, *root.parents):
        info = stamp(path)
        if not stat.S_ISDIR(info.mode):
            raise ValueError("runtime_selection_unsafe")
        _safe(path, info)
    releases = root / "releases"
    _safe(releases, stamp(releases))
    if releases.resolve() != releases or not releases.is_dir():
        raise ValueError("runtime_selection_unsafe")
    selected = stamp(root / "current")
    _safe(selected.path, selected)
    if not stat.S_ISLNK(selected.mode):
        raise ValueError("runtime_selection_unsafe")
    target = selected.path.resolve(strict=True)
    raw_target = Path(str(selected.link_target))
    declared = raw_target if raw_target.is_absolute() else root / raw_target
    if target.parent != releases or declared != target:
        raise ValueError("runtime_selection_unsafe")
    return target, selected


def _children(path: Path, remaining: int, deadline: float) -> list[Path]:
    """Collect only the remaining finite inventory before sorting names."""
    children: list[Path] = []
    with os.scandir(path) as entries:
        for child in entries:
            if len(children) >= remaining:
                raise ValueError("runtime_structure_limit")
            if time.monotonic() >= deadline:
                raise ValueError("runtime_probe_unavailable")
            children.append(Path(child.path))
    return sorted(children, reverse=True)


def _inventory(
    root: Path, deadline: float
) -> tuple[tuple[FileStamp, ...], str]:
    """Hash all package files, safe internal links and directories."""
    pending = [root]
    stamps: list[FileStamp] = []
    rows: list[tuple[str, str]] = []
    remaining = PACKAGE_LIMIT
    while pending:
        if time.monotonic() >= deadline:
            raise ValueError("runtime_probe_unavailable")
        path = pending.pop()
        relative = path.relative_to(root)
        if len(stamps) + len(pending) >= 256 or len(relative.parts) > 8:
            raise ValueError("runtime_structure_limit")
        info = stamp(path)
        _safe(path, info)
        stamps.append(info)
        if stat.S_ISDIR(info.mode):
            pending.extend(
                _children(path, 256 - len(stamps) - len(pending), deadline)
            )
            content = "directory"
        elif stat.S_ISREG(info.mode):
            if path.stat().st_nlink != 1:
                raise ValueError("runtime_selection_unsafe")
            content, size = hash_file(
                path, min(FILE_LIMIT, remaining), deadline
            )
            remaining -= size
        elif stat.S_ISLNK(info.mode):
            target = path.resolve(strict=True)
            if (
                root not in target.parents
                or target in path.parents
                or not (target.is_file() or target.is_dir())
            ):
                raise ValueError("runtime_selection_unsafe")
            content = "link:" + str(info.link_target)
        else:
            raise ValueError("runtime_selection_unsafe")
        rows.append((str(relative), content))
    return (
        tuple(stamps),
        hashlib.sha256(canonical_json_value(rows).encode()).hexdigest(),
    )


def _layout(root: Path) -> PackageLayout:
    """Read only the finite standalone layout declaration."""
    with open_regular(root / "codex-package.json") as checked:
        raw = os.read(checked.fileno(), 65537)
    if len(raw) > 65536:
        raise ValueError("runtime_structure_limit")
    data: object = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError("runtime_interface_unsupported")
    known = {
        field.alias or name
        for name, field in PackageLayout.model_fields.items()
    }
    try:
        layout = PackageLayout.model_validate(
            {
                key: value
                for key, value in cast(dict[str, object], data).items()
                if key in known
            }
        )
    except ValidationError:
        raise ValueError("runtime_interface_unsupported") from None
    if (
        not all(
            (root / name).is_file()
            for name in (
                layout.entrypoint,
                layout.exec_server,
            )
        )
        or not (root / layout.resources_dir).is_dir()
        or not (root / layout.path_dir).is_dir()
    ):
        raise ValueError("runtime_interface_unsupported")
    return layout


def observe(
    home: Path, selection: ManagedCodexSelection, *, deadline: float
) -> RuntimeIdentity:
    """Observe the package without copying, probing or authorizing it."""
    del home
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("runtime_interface_unsupported")
    deadline = min(deadline, time.monotonic() + 60.0)
    try:
        root, selected = _root(selection)
        stamps, package_hash = _inventory(root, deadline)
        layout = _layout(root)
        executable, hashed = review_codex_launch.verify_codex_executable(
            root / layout.entrypoint,
            limit=FILE_LIMIT,
            deadline=deadline,
        )
        schema = hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest()
        data = RuntimeIdentity(
            requested=RunnerConfig(
                runner="codex_cli",
                executable=str(executable),
                version=layout.version,
                model=selection.model,
                reasoning=selection.reasoning,
            ),
            package_root=root,
            executable_sha256=hashed,
            package_sha256=package_hash,
            platform="Darwin/arm64",
            selector_stamp=selected,
            file_stamps=stamps,
            recipe_digest=RECIPE_DIGEST,
            schema_digest=schema,
            validator_digest=VALIDATOR_DIGEST,
            capability_key="0" * 64,
        )
        data = data.model_copy(
            update={
                "capability_key": hashlib.sha256(
                    canonical_json(data).encode()
                ).hexdigest()
            }
        )
        if not matches(data) or time.monotonic() >= deadline:
            raise ValueError("runtime_selection_unavailable")
        return data
    except FileNotFoundError:
        raise ValueError("runtime_selection_unavailable") from None
    except (OSError, RuntimeError):
        raise ValueError("runtime_selection_unsafe") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("runtime_interface_unsupported") from None
