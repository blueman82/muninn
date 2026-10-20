"""Publish validated candidates and retain possible-exposure records."""

from __future__ import annotations

import dataclasses
import json
import os
from contextlib import closing
from pathlib import Path

from install import legacy_exclusion, legacy_state, record, snapshot_io
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_rebuild, store, store_fingerprint
from muninn import review_quiescence as q
from muninn import review_quiescence_state as state


def encoded(value: object) -> object:
    """Normalize tuples to durable JSON values for exact comparisons."""
    return json.loads(json.dumps(value))


def check_archive(ctx: Ctx, rec: Record) -> None:
    """Keep the exact completed archive without permitting arbitrary paths."""
    archive = rec["legacy"]["archive"]
    path = Path(archive["path"])
    if path.parent != ctx.data or not path.name.startswith(
        store.PRE_UPGRADE_PREFIX
    ):
        raise StepFailedError("legacy_archive_changed")
    if encoded(
        dataclasses.asdict(store_fingerprint.capture_file(path))
    ) != encoded(archive["file"]):
        raise StepFailedError("legacy_archive_changed")


def _authority(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck
) -> tuple[int, int]:
    """Revalidate held operation identity and retained launch exclusion."""
    identity = check()
    legacy_state.retained(ctx, rec)
    legacy_exclusion.verify(ctx, rec)
    if os.path.lexists(ctx.plist):
        raise StepFailedError("legacy_service_returned")
    check()
    return identity


def publish(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    original: store_fingerprint.Fingerprint,
    candidate: Path,
    check: legacy_state.OperationCheck,
) -> None:
    """Persist possible exposure before one replacement and witness write."""
    if not owner.writer_held:
        raise RuntimeError("maintenance_writer_required")
    if any(
        state.inspect(ctx.data, name)[1] is not None
        for name in (state.WITNESS, state.BASELINE)
    ):
        raise state.QuiescenceRefusedError("quiescence_unverified")
    operation_identity = _authority(ctx, rec, check)
    legacy = rec["legacy"]
    check_archive(ctx, rec)
    store_fingerprint.verify(ctx.data, original)
    legacy["publication"] = "possible"
    legacy["phase"] = "publication_possible"
    legacy["publication_binding"] = {
        "source": dataclasses.asdict(original),
        "candidate": dataclasses.asdict(
            store_fingerprint.capture_file(candidate)
        ),
        "candidate_path": str(candidate),
        "owner_lock": list(state.lock_identity(ctx.data, owner.fd)),
        "operation_lock": list(operation_identity),
        "runtime": legacy["candidate"],
    }
    record.save(ctx, rec)
    store_fingerprint.verify(ctx.data, original)
    check_archive(ctx, rec)
    if encoded(
        dataclasses.asdict(store_fingerprint.capture_file(candidate))
    ) != encoded(legacy["publication_binding"]["candidate"]):
        raise StepFailedError("legacy_candidate_changed")
    _authority(ctx, rec, check)
    platform_rebuild.publish_replacement(
        ctx.data, store.db_path(ctx.data), candidate
    )
    witness(ctx, rec, owner, check)
    verify_live(ctx, rec, owner)
    legacy["publication"] = "complete"
    legacy["phase"] = "published"
    record.save(ctx, rec)


def witness(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    check: legacy_state.OperationCheck,
) -> None:
    """Publish the initial witness while exclusion remains exact."""
    identity = state.lock_identity(ctx.data, owner.fd)
    if list(identity) != rec["legacy"]["publication_binding"]["owner_lock"]:
        raise StepFailedError("legacy_owner_lock_changed")
    if any(
        state.inspect(ctx.data, name)[1] is not None
        for name in (state.WITNESS, state.BASELINE)
    ):
        raise state.QuiescenceRefusedError("quiescence_unverified")
    _authority(ctx, rec, check)
    state.publish(
        ctx.data,
        owner.fd,
        state.WITNESS,
        state.Witness(
            version=1, lock_identity=identity, owner=None, state="complete"
        ),
    )


def verify_live(ctx: Ctx, rec: Record, owner: q.MaintenanceOwner) -> None:
    """Reopen the actual replacement under the continuously held writer."""
    path = store.db_path(ctx.data)
    expected = rec["legacy"]["publication_binding"]["candidate"]["identity"]
    info = path.lstat()
    if [info.st_dev, info.st_ino] != list(expected):
        raise StepFailedError("legacy_live_changed")
    with (
        snapshot_io.validated(path) as check,
        closing(store.connect_rw(path)) as conn,
    ):
        check()
        q.verify_current(owner, conn)
        check()
