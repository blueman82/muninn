"""Copy the exact stopped legacy store into an unpublished governed target."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from install import (
    legacy_exclusion,
    legacy_publication,
    legacy_state,
    record,
    snapshot_io,
)
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
from muninn import review_pause as q
from muninn.tombstone_intent import read_intent
from muninn.tombstone_replay import plan_transcripts


def validate_target(conn: sqlite3.Connection, home: Path) -> None:
    """Require complete erasure, exact schema, integrity and matching FTS."""
    tombstones.require_current_tombstones(conn, home)
    store_copy.validate_source(conn)
    conn.execute(
        "INSERT INTO event_fts(event_fts,rank) VALUES('integrity-check',1)"
    )
    if conn.execute("SELECT 1 FROM knowledge LIMIT 1").fetchone():
        raise StepFailedError("legacy_knowledge_imported")


def prepare(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    operation_check: legacy_state.OperationCheck,
) -> Path:
    """Recheck closure and publish beneath one retained physical owner."""
    operation_check()
    legacy_state.retained(ctx, rec)
    legacy_exclusion.verify(ctx, rec)
    legacy_exclusion.service_absent(ctx)
    if legacy_state.boot(ctx) != rec["legacy"]["resumed_boot"]:
        raise StepFailedError("legacy_boot_changed")
    path = store.db_path(ctx.data)
    with q.maintenance_writer(owner):
        original = store_fingerprint.capture(ctx.data)
        if (
            json.loads(json.dumps(dataclasses.asdict(original)))
            != rec["legacy"]["source"]
        ):
            raise StepFailedError("legacy_source_changed")
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
            if store_copy.validate_source(source) != 3:
                raise StepFailedError("cutover_source_mismatch")
            plan_transcripts(source, read_intent(source, ctx.data))
            check()
        store_fingerprint.verify(ctx.data, original)
        archive = platform_rebuild.retain_upgrade_store(ctx.data, path)
        rec["legacy"]["archive"] = {
            "path": str(archive),
            "file": dataclasses.asdict(
                store_fingerprint.capture_file(archive)
            ),
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
                rec["legacy"]["copy_counts"] = store_copy.copy_governed_state(
                    source, target
                )
                tombstones.reapply_tombstones(target, ctx.data)
                validate_target(target, ctx.data)
            check()
        file_sync.sync_path(candidate)
        store_fingerprint.verify(ctx.data, original)
        legacy_publication.publish(
            ctx, rec, owner, original, candidate, operation_check
        )
    return path
