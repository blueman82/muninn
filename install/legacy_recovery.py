"""Advance retained legacy operations only from proven successor states."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from install import legacy_baseline as baseline
from install import (
    legacy_cutover,
    legacy_state,
    lifecycle,
    record,
    review_policy,
)
from install import legacy_exclusion as exclusion
from install import legacy_publication as publication
from install.context import Ctx, StepFailedError, job, link_text
from install.record import Record
from install.steps_config import claude, codex, cursor
from install.steps_release import pin, relink
from muninn import file_sync, store, store_fingerprint
from muninn import review_pause as q
from muninn import review_pause_state as state


def configuration(ctx: Ctx) -> Record:
    """Bind selected compatible links and the actual provider configuration."""
    files = {}
    for name, path in (
        ("claude", ctx.settings),
        ("codex", ctx.config),
        ("cursor", ctx.cursor_settings),
    ):
        files[name] = (
            baseline.file_state(path) if os.path.lexists(path) else None
        )
    return {
        "files": files,
        "cache": (
            baseline.tree_state(ctx.cache, private=False)
            if ctx.cache.exists()
            else None
        ),
        "current": link_text(ctx.lib / "current"),
        "python": link_text(ctx.lib / "python"),
    }


def retained(ctx: Ctx, rec: Record) -> None:
    """Validate retained artifacts after intentional source changes."""
    legacy_state.validate(ctx, rec)
    legacy_state.retained(ctx, rec)
    value = rec["legacy"]
    if (
        os.path.lexists(ctx.lib / baseline.BASELINE)
        or baseline.tree_state(exclusion.retained(ctx))
        != value["before"]["runtime"]
        or baseline.file_state(ctx.rdir / "legacy-service")
        != value["retained_service"]
        or baseline.file_state(ctx.rdir / "maintenance", private=True)
        != value["stub"]
    ):
        raise StepFailedError("legacy_exclusion_changed")
    bound = value["publication_binding"]
    if (
        baseline.file_state(ctx.lib / ".install-record.lock", private=True)[
            "stamp"
        ][:2]
        != bound["operation_lock"]
    ):
        raise StepFailedError("legacy_operation_lock_changed")
    publication.check_archive(ctx, rec)
    review_policy.frozen(ctx, rec)


def _service(ctx: Ctx, rec: Record) -> None:
    """Stop only the verified compatible service after possible startup."""
    if rec["legacy"]["phase"] != "starting" or not ctx.plist.exists():
        exclusion.service_absent(ctx)
        return
    expected = (
        (ctx.lib / rec["sha"] / "launchd/com.muninn.plist").read_bytes()
        if ctx.platform == "darwin"
        else lifecycle.linux_unit(
            ctx, Path(rec["python"]["path"]), ctx.release
        ).encode()
    )
    if baseline.read_bytes(ctx.plist) != expected:
        raise StepFailedError("legacy_service_changed")
    if ctx.dry_run:
        return
    lifecycle.stop(ctx)
    if job(ctx) is not None:
        raise StepFailedError("legacy_service_returned")


def verify_resume(ctx: Ctx, rec: Record) -> None:
    """Validate successor paths without replaying original-only validators."""
    retained(ctx, rec)
    _service(ctx, rec)
    value = rec["legacy"]
    if value["phase"] == "configuring":
        raise StepFailedError("legacy_configuration_incomplete")
    if value.get("configuration") is not None:
        if configuration(ctx) != value["configuration"]:
            raise StepFailedError("legacy_configuration_changed")
    elif value.get("compatible_exposure_possible"):
        raise StepFailedError("legacy_configuration_incomplete")
    else:
        exclusion.verify(ctx, rec)
        original = value.get("boot")
        legacy_state.validate_boot(ctx, original)
        if legacy_state.boot(ctx) == original:
            raise StepFailedError("restart_required")
    launcher = exclusion.selection(ctx)
    if value["phase"] in {"launcher_possible", "activated", "starting"}:
        if launcher["text"] not in {
            str(ctx.rdir / "maintenance"),
            str(ctx.lib / "current/bin/muninn"),
        }:
            raise StepFailedError("legacy_launcher_changed")
    elif launcher != value["launcher"]:
        raise StepFailedError("legacy_launcher_changed")


def prepare(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    check: legacy_state.OperationCheck,
) -> Path:
    """Recognize actual replacement and complete only safe forward recovery."""
    check()
    retained(ctx, rec)
    path = store.db_path(ctx.data)
    value = rec["legacy"]
    with q.maintenance_writer(owner):
        actual = store_fingerprint.capture(ctx.data)
        bound = value["publication_binding"]
        retry = publication.encoded(
            dataclasses.asdict(actual)
        ) == publication.encoded(bound["source"])
        if retry and value.get("compatible_exposure_possible"):
            raise StepFailedError("legacy_source_ambiguous")
    if retry:
        exclusion.verify(ctx, rec)
        value.setdefault("retained_archives", []).append(value["archive"])
        value["publication"] = "not_started"
        value["phase"] = "verified"
        value["resumed_boot"] = legacy_state.boot(ctx)
        record.save(ctx, rec)
        return legacy_cutover.prepare(ctx, rec, owner, check)
    with q.maintenance_writer(owner):
        actual = store_fingerprint.capture(ctx.data)
        expected_key = publication.encoded(bound["source"]["key"])
        found_key = (
            publication.encoded(dataclasses.asdict(actual.key))
            if actual.key is not None
            else None
        )
        if found_key != expected_key:
            raise StepFailedError("legacy_key_changed")
        if list(actual.database.identity) != list(
            bound["candidate"]["identity"]
        ):
            raise StepFailedError("legacy_live_changed")
        raw, found = state.inspect(ctx.data, state.WITNESS)
        if found is None:
            if (
                value.get("compatible_exposure_possible")
                or actual.database.digest != bound["candidate"]["digest"]
            ):
                raise state.PauseRefusedError("pause_unverified")
            exclusion.verify(ctx, rec)
            publication.witness(ctx, rec, owner, check)
        elif state.parse(raw, state.Witness) is None:
            raise state.PauseRefusedError("pause_unverified")
        publication.verify_live(ctx, rec, owner)
        check()
        retained(ctx, rec)
        check()
        value["publication"] = "complete"
        if value["phase"] == "publication_possible":
            value["phase"] = "published"
        record.save(ctx, rec)
    return path


def configure(ctx: Ctx, rec: Record) -> None:
    """Synchronize compatible selection and config while retaining the stub."""
    value = rec["legacy"]
    if value.get("configuration") is not None:
        if configuration(ctx) != value["configuration"]:
            raise StepFailedError("legacy_configuration_changed")
        return
    if value.get("compatible_exposure_possible"):
        raise StepFailedError("legacy_configuration_incomplete")
    retained(ctx, rec)
    exclusion.verify(ctx, rec)
    value["phase"] = "configuring"
    value["compatible_exposure_possible"] = True
    record.save(ctx, rec)
    pin(ctx, rec)
    for step in (claude, codex, cursor):
        step(ctx, rec)
        rec["steps"].append(step.__name__)
        record.save(ctx, rec)
    for path in (ctx.settings, ctx.config, ctx.cursor_settings):
        if path.exists():
            file_sync.sync_path(path)
            file_sync.sync_path(path.parent)
    file_sync.sync_path(ctx.lib)
    retained(ctx, rec)
    value["configuration"] = configuration(ctx)
    if (
        value["configuration"]["current"] != rec["sha"]
        or value["configuration"]["python"] != rec["python"]["path"]
    ):
        raise StepFailedError("legacy_selection_changed")
    value["phase"] = "configured"
    record.save(ctx, rec)


def verify_configuration(ctx: Ctx, rec: Record) -> None:
    """Require retained runtime and configured state before activation."""
    retained(ctx, rec)
    if configuration(ctx) != rec["legacy"].get("configuration"):
        raise StepFailedError("legacy_configuration_changed")


def expose(ctx: Ctx, rec: Record, check: legacy_state.OperationCheck) -> None:
    """Remove the maintenance launcher last, after final policy completion."""
    verify_configuration(ctx, rec)
    value = rec["legacy"]
    if exclusion.selection(ctx)["text"] == str(ctx.lib / "current/bin/muninn"):
        if value["phase"] not in {
            "launcher_possible",
            "activated",
            "starting",
        }:
            raise StepFailedError("legacy_launcher_changed")
        return
    if exclusion.selection(ctx) != value["launcher"]:
        raise StepFailedError("legacy_launcher_changed")
    value["phase"] = "launcher_possible"
    record.save(ctx, rec)
    check()
    relink(ctx.muninn, ctx.lib / "current/bin/muninn", ctx.ts)
    file_sync.sync_path(ctx.muninn.parent)
    value["phase"] = "activated"
    record.save(ctx, rec)
