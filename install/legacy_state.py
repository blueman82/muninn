"""Installer-private serialization and bounded pending legacy records."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from install import legacy_baseline as baseline
from install.context import Ctx, StepFailedError
from install.record import Record, install_record, save
from muninn import file_sync, platform_io, platform_lock, review_boot_identity
from muninn.review_pause_state import PauseRefusedError

OperationCheck = Callable[[], tuple[int, int]]


@contextmanager
def operation(ctx: Ctx) -> Generator[OperationCheck]:
    """Serialize mutating installer entries before either maintenance root."""
    if ctx.dry_run:
        yield lambda: (0, 0)
        return
    path = ctx.lib / ".install-record.lock"
    if os.name == "nt":
        if ctx.lib.is_symlink() or not platform_io.is_private(
            ctx.lib, directory=True
        ):
            raise StepFailedError("install_root_unsafe")
    else:
        baseline.directory(ctx.lib)
    expected = path.lstat() if os.path.lexists(path) else None
    if expected is not None and (
        not stat.S_ISREG(expected.st_mode)
        or not platform_io.is_private(path)
        or expected.st_nlink != 1
        or (os.name != "nt" and expected.st_uid != os.getuid())
    ):
        raise StepFailedError("install_lock_unsafe")
    fd = platform_lock.acquire(path)
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if expected and (expected.st_dev, expected.st_ino) != identity:
            raise StepFailedError("install_lock_changed")
        file_sync.sync_fd(fd)
        if os.name != "nt":
            file_sync.sync_path(ctx.lib)
        check_lock(path, identity)
        yield lambda: check_lock(path, identity)
        check_lock(path, identity)
    finally:
        os.close(fd)


def check_lock(path: Path, identity: tuple[int, int]) -> tuple[int, int]:
    """Refuse a substituted lock pathname while retaining the original fd."""
    found = path.lstat()
    if (
        not stat.S_ISREG(found.st_mode)
        or (found.st_dev, found.st_ino) != identity
        or not platform_io.is_private(path)
        or found.st_nlink != 1
        or (os.name != "nt" and found.st_uid != os.getuid())
    ):
        raise StepFailedError("install_lock_changed")
    return identity


def read(path: Path) -> Record:
    """Read bounded private JSON without following links."""
    before = baseline.file_state(path, private=True)
    with platform_io.open_regular(path) as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if (
        len(raw) > 4 * 1024 * 1024
        or baseline.file_state(path, private=True) != before
    ):
        raise StepFailedError("legacy_record_unsafe")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StepFailedError("legacy_record_invalid")
    return cast(Record, value)


def validate(ctx: Ctx, rec: Record) -> None:
    """Require the exact versioned record and derived operation location."""
    if ctx.platform not in ("darwin", "linux"):
        raise StepFailedError("legacy_boot_unsupported")
    raw = rec.get("legacy")
    if not isinstance(raw, dict):
        raise StepFailedError("legacy_record_invalid")
    state = cast(Record, raw)
    if (
        state.get("version") != 1
        or state.get("baseline") != baseline.BASELINE
        or state.get("platform") != ctx.platform
        or state.get("phase")
        not in {
            "intent",
            "prepared",
            "launcher_intent",
            "launcher",
            "runtime_intent",
            "runtime",
            "service_intent",
            "service",
            "armed",
            "verified",
            "recovery_required",
            "publication_possible",
            "published",
            "configuring",
            "configured",
            "launcher_possible",
            "activated",
            "starting",
            "complete",
        }
        or state.get("publication")
        not in {"not_started", "possible", "complete"}
        or not isinstance(rec.get("ts"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rec["ts"])
        or rec.get("ts") != ctx.ts
        or rec.get("rdir") != str(ctx.rdir)
        or rec.get("home") != str(ctx.home)
        or rec.get("upgrade") is not True
        or rec.get("fresh") is not False
        or not isinstance(rec.get("sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", rec["sha"])
    ):
        raise StepFailedError("legacy_record_invalid")
    if state.get("publication") != "not_started" and not isinstance(
        state.get("publication_binding"), dict
    ):
        raise StepFailedError("legacy_record_invalid")
    baseline.directory(ctx.rdir, private=True)
    if state.get("record_directory") != baseline.directory(
        ctx.rdir, private=True
    ):
        raise StepFailedError("legacy_record_changed")


def pending(ctx: Ctx) -> tuple[Ctx, Record] | None:
    """Discover one pending record before normal upgrade preflight."""
    if ctx.platform == "win32":
        return None
    candidates: list[tuple[Ctx, Record]] = []
    paths = sorted(ctx.rdir.parent.glob("muninn-install-*"))
    if len(paths) > 128:
        raise StepFailedError("legacy_records_ambiguous")
    for directory in paths:
        baseline.directory(directory, private=True)
        path = directory / "rollback-record.json"
        if not path.exists():
            raise StepFailedError("legacy_record_missing")
        rec = read(path)
        if "legacy" not in rec:
            if any(
                os.path.lexists(directory / name)
                for name in ("maintenance", "legacy-release", "legacy-service")
            ):
                raise StepFailedError("legacy_record_invalid")
            continue
        if not isinstance(rec.get("ts"), str):
            raise StepFailedError("legacy_record_invalid")
        active = dataclasses.replace(ctx, ts=rec["ts"])
        validate(active, rec)
        if directory != active.rdir:
            raise StepFailedError("legacy_record_invalid")
        if rec["legacy"].get("phase") != "complete":
            candidates.append((active, rec))
    _summary(ctx, candidates)
    return candidates[0] if candidates else None


def _summary(ctx: Ctx, candidates: list[tuple[Ctx, Record]]) -> None:
    """Cross-check pending discovery against the durable summary linkage."""
    summary_path = ctx.lib / "install-record.json"
    summary = read(summary_path) if os.path.lexists(summary_path) else {}
    if len(candidates) > 1:
        raise StepFailedError("legacy_records_ambiguous")
    if (
        summary.get("legacy")
        and not candidates
        and summary.get("outcome") not in {"rolled_back", "ok"}
    ):
        raise StepFailedError("legacy_record_missing")
    if (
        candidates
        and summary.get("legacy")
        and summary.get("record")
        != str(candidates[0][0].rdir / "rollback-record.json")
    ):
        raise StepFailedError("legacy_record_changed")


def phase(ctx: Ctx, rec: Record, name: str) -> None:
    """Persist the next action's intent before its external effect."""
    rec["legacy"]["phase"] = name
    save(ctx, rec)


def outcome(ctx: Ctx, rec: Record, name: str) -> None:
    """Publish a bounded outcome without claiming upgrade completion."""
    rec["legacy_outcome"] = name
    save(ctx, rec)
    install_record(ctx, rec, name)
    ctx.say(name)


def boot(ctx: Ctx) -> str:
    """Require a canonical supported kernel boot observation."""
    if ctx.platform not in ("darwin", "linux"):
        raise StepFailedError("legacy_boot_unsupported")
    try:
        value = review_boot_identity.observe()
    except PauseRefusedError as exc:
        raise StepFailedError("legacy_boot_unavailable") from exc
    validate_boot(ctx, value)
    return value


def validate_boot(ctx: Ctx, value: object) -> None:
    """Reject unknown, malformed or unsupported serialized boot evidence."""
    pattern = ctx.platform + r":[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    if (
        not isinstance(value, str)
        or re.fullmatch(pattern, value) is None
        or value.split(":")[1].replace("-", "") == "0" * 32
    ):
        raise StepFailedError("legacy_boot_unavailable")


def retained(ctx: Ctx, rec: Record) -> None:
    """Verify home, repository and prepared candidate on resume."""
    before = rec["legacy"]["before"]
    if (
        baseline.directory(ctx.home) != before["home"]
        or baseline.directory(ctx.lib, private=True) != before["library"]
        or baseline.directory(Path(rec["repo"])) != before["repo"]
        or baseline.tree_state(ctx.lib / rec["sha"], links=True)
        != rec["legacy"]["candidate"]
    ):
        raise StepFailedError("legacy_installation_changed")
