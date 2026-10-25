"""Convert a supported historical store while retaining its full source."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from install import legacy_cutover, record, snapshot_io
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import (
    file_sync,
    platform_io,
    platform_rebuild,
    store,
    store_copy,
    store_fingerprint,
    tombstones,
)
from muninn import (
    review_pause as q,
)
from muninn import (
    review_pause_state as state,
)
from muninn.tombstone_intent import read_intent
from muninn.tombstone_replay import plan_transcripts


def _source(ctx: Ctx, path: Path) -> None:
    """Require a validated v3 source and plan erasure before copying."""
    try:
        with (
            snapshot_io.validated(path) as check,
            closing(
                sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True, isolation_level=None
                )
            ) as conn,
        ):
            check()
            conn.row_factory = sqlite3.Row
            if (
                store_copy.validate_source(conn) != 3
                or conn.execute("PRAGMA journal_mode").fetchone()[0]
                != "delete"
            ):
                raise StepFailedError("cutover_source_mismatch")
            plan_transcripts(conn, read_intent(conn, ctx.data))
            check()
    except (sqlite3.Error, ValueError, store.StoreUnavailableError) as exc:
        raise StepFailedError("cutover_source_mismatch") from exc


def _file_matches(path: Path, expected: dict[str, object], code: str) -> None:
    """Refuse publication if a private migration file changed."""
    found = dataclasses.asdict(store_fingerprint.capture_file(path))
    if found != expected:
        raise StepFailedError(code)


def prepare(ctx: Ctx, rec: Record, owner: q.MaintenanceOwner) -> Path:
    """Copy schema v3 state into a marked candidate under one writer fence."""
    path = store.db_path(ctx.data)
    with q.maintenance_writer(owner):
        original = store_fingerprint.capture(ctx.data)
        _source(ctx, path)
        store_fingerprint.verify(ctx.data, original)
        archive = platform_rebuild.retain_upgrade_store(ctx.data, path)
        archive_fingerprint = dataclasses.asdict(
            store_fingerprint.capture_file(archive)
        )
        rec["store"] = {
            "phase": "legacy_archived",
            "path": str(path),
            "file_identity": list(original.database.identity),
            "archive": str(archive),
            "archive_fingerprint": archive_fingerprint,
        }
        record.save(ctx, rec)

        candidate = ctx.data / (
            store.RECOVERY_PREFIX + "candidate-" + uuid.uuid4().hex
        )
        fd = platform_io.open_private(
            candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
        os.close(fd)
        with (
            snapshot_io.validated(path) as check,
            closing(
                sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True, isolation_level=None
                )
            ) as source,
        ):
            check()
            source.row_factory = sqlite3.Row
            with closing(store.connect_rw(candidate)) as target:
                counts = store_copy.copy_governed_state(source, target)
                tombstones.reapply_tombstones(target, ctx.data)
                legacy_cutover.validate_target(target, ctx.data)
            check()
        file_sync.sync_path(candidate)
        store_fingerprint.verify(ctx.data, original)
        _file_matches(archive, archive_fingerprint, "cutover_archive_changed")
        if any(
            state.inspect(ctx.data, name)[1] is not None
            for name in (state.WITNESS, state.BASELINE)
        ):
            raise StepFailedError("pause_unverified")

        candidate_fingerprint = dataclasses.asdict(
            store_fingerprint.capture_file(candidate)
        )
        rec["store"].update(
            phase="migration_possible",
            candidate_path=str(candidate),
            candidate_fingerprint=candidate_fingerprint,
            file_identity=candidate_fingerprint["identity"],
            copy_counts=counts,
        )
        record.save(ctx, rec)
        store_fingerprint.verify(ctx.data, original)
        _file_matches(archive, archive_fingerprint, "cutover_archive_changed")
        _file_matches(
            candidate, candidate_fingerprint, "cutover_candidate_changed"
        )
        platform_rebuild.publish_replacement(ctx.data, path, candidate)
        with (
            snapshot_io.validated(path) as check,
            closing(store.connect_rw(path)) as conn,
        ):
            check()
            store.assert_store_format(conn)
            state.publish(
                ctx.data,
                owner.fd,
                state.WITNESS,
                state.Witness(
                    version=1,
                    lock_identity=state.lock_identity(ctx.data, owner.fd),
                    owner=None,
                    state="complete",
                ),
            )
            q.verify_current(owner, conn)
            check()
        file_sync.sync_path(path)
        rec["store"]["phase"] = "current_prepared"
        record.save(ctx, rec)
    return path
