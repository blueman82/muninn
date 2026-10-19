"""Reversible terminal launch fences for the exact old installation."""

from __future__ import annotations

import os
from pathlib import Path

from install import legacy_baseline as baseline
from install import legacy_state as state
from install import lifecycle
from install.context import Ctx, StepFailedError, job, link_text, must
from install.record import Record, save
from install.release_io import publish, write_private
from install.steps_release import relink
from muninn import file_sync
from muninn.obs_linux_service import query_unit

STUB = (
    b"#!/bin/sh\n"
    b"printf '%s\\n' "
    b'\'{"error":"maintenance","reason":"restart_required"}\'\n'
    b'if [ "${1-}" = hook ]; then exit 0; fi\n'
    b"exit 1\n"
)


def retained(ctx: Ctx) -> Path:
    """Derive the retained runtime path inside the operation directory."""
    return ctx.rdir / "legacy-release"


def selection(ctx: Ctx) -> dict[str, object]:
    """Read the stable launcher link without resolving the maintenance stub."""
    info = ctx.muninn.lstat()
    if not ctx.muninn.is_symlink() or info.st_uid != os.getuid():
        raise StepFailedError("legacy_launcher_changed")
    return {
        "text": link_text(ctx.muninn),
        "identity": [info.st_dev, info.st_ino],
    }


def fence_runtime(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Publish the terminal launcher and move the old import root aside."""
    before = rec["legacy"]["before"]
    check()
    if baseline.installation(ctx, rec) != before:
        raise StepFailedError("legacy_installation_changed")
    state.phase(ctx, rec, "launcher_intent")
    stub = ctx.rdir / "maintenance"
    write_private(stub, STUB)
    stub.chmod(0o700)
    file_sync.sync_path(stub)
    file_sync.sync_path(ctx.rdir)
    rec["legacy"]["stub"] = baseline.file_state(stub, private=True)
    save(ctx, rec)
    check()
    relink(ctx.muninn, stub, ctx.ts)
    file_sync.sync_path(ctx.muninn.parent)
    rec["legacy"]["launcher"] = selection(ctx)
    state.phase(ctx, rec, "launcher")
    check()
    if (
        baseline.expected_release(ctx, ctx.lib / baseline.BASELINE)
        != before["runtime"]
    ):
        raise StepFailedError("legacy_runtime_changed")
    state.phase(ctx, rec, "runtime_intent")
    publish(
        ctx.lib / baseline.BASELINE,
        retained(ctx),
        replace=False,
        directory=True,
    )
    file_sync.sync_path(ctx.lib)
    state.phase(ctx, rec, "runtime")


def stop_service(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Remove the persistent registration outside both maintenance roots."""
    check()
    if baseline.file_state(ctx.plist) != rec["legacy"]["before"]["service"]:
        raise StepFailedError("legacy_service_changed")
    state.phase(ctx, rec, "service_intent")
    lifecycle.stop(ctx)
    check()
    if baseline.file_state(ctx.plist) != rec["legacy"]["before"]["service"]:
        raise StepFailedError("legacy_service_changed")
    publish(ctx.plist, ctx.rdir / "legacy-service", replace=False)
    file_sync.sync_path(ctx.plist.parent)
    moved = baseline.file_state(ctx.rdir / "legacy-service")
    original = rec["legacy"]["before"]["service"]
    if (
        moved["stamp"][:-1] != original["stamp"][:-1]
        or moved["sha256"] != original["sha256"]
    ):
        raise StepFailedError("legacy_service_changed")
    rec["legacy"]["retained_service"] = moved
    if ctx.platform == "linux":
        must(ctx, ["systemctl", "--user", "daemon-reload"], quiet=True)
    service_absent(ctx)
    state.phase(ctx, rec, "service")


def service_absent(ctx: Ctx) -> None:
    """Require absent persistent and native service registrations."""
    if os.path.lexists(ctx.plist):
        raise StepFailedError("legacy_service_returned")
    if ctx.platform == "linux":
        registration = query_unit(ctx.run)
        if registration != {
            "LoadState": "not-found",
            "MainPID": "0",
            "ActiveState": "inactive",
            "FragmentPath": "",
            "DropInPaths": "",
        }:
            raise StepFailedError("legacy_service_returned")
    elif job(ctx) is not None:
        raise StepFailedError("legacy_service_returned")


def verify(ctx: Ctx, rec: Record) -> None:
    """Verify retained launch fences against their original identities."""
    legacy = rec["legacy"]
    before = legacy["before"]
    if os.path.lexists(ctx.lib / baseline.BASELINE):
        raise StepFailedError("legacy_runtime_returned")
    if (
        baseline.tree_state(retained(ctx)) != before["runtime"]
        or baseline.file_state(ctx.rdir / "legacy-service")
        != legacy["retained_service"]
        or baseline.file_state(ctx.rdir / "maintenance", private=True)
        != legacy["stub"]
        or selection(ctx) != legacy["launcher"]
        or selection(ctx)["text"] != str(ctx.rdir / "maintenance")
        or baseline.provider_state(ctx, retained(ctx)) != before["providers"]
    ):
        raise StepFailedError("legacy_exclusion_changed")
    for name in ("current", "python"):
        path = ctx.lib / name
        info = path.lstat()
        if (
            not path.is_symlink()
            or {
                "text": link_text(path),
                "identity": [info.st_dev, info.st_ino],
            }
            != before["links"][name]
        ):
            raise StepFailedError("legacy_selection_changed")
    interpreter = before["links"]["interpreter"]
    if baseline.file_state(Path(interpreter["path"])) != interpreter["file"]:
        raise StepFailedError("legacy_interpreter_changed")


def contain(ctx: Ctx, rec: Record, check: state.OperationCheck) -> None:
    """Finish stopping a positively identified partial managed exclusion."""
    before = rec["legacy"]["before"]
    changed = (
        not os.path.lexists(ctx.lib / baseline.BASELINE)
        or selection(ctx) != before["links"]["launcher"]
    )
    if not changed:
        return
    if os.path.lexists(ctx.plist):
        stop_service(ctx, rec, check)
    else:
        check()
        service_absent(ctx)
    rec["legacy"]["containment"] = "service_absent"
