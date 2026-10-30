"""Migrate exact marked-one governed state with a raw recoverable archive."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from install import decision_source, record, snapshot_io, store_migration
from install.context import Ctx, StepFailedError
from install.policy_source_format import validate_policy_source
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


def _file_matches(path: Path, expected: dict[str, object], code: str) -> None:
    """Refuse publication if a private migration file changed."""
    if dataclasses.asdict(store_fingerprint.capture_file(path)) != expected:
        raise StepFailedError(code)


@contextmanager
def _source(owner: q.MaintenanceOwner) -> Generator[sqlite3.Connection]:
    """Read checked old state inside the caller's one active writer region."""
    path = store.db_path(owner.home)
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
        decision_source.verify_source(owner, source)
        try:
            yield source
            check()
        except BaseException:
            with suppress(BaseException):
                source.close()
            raise


def _candidate(owner: q.MaintenanceOwner, path: Path) -> dict[str, int]:
    """Build a complete unpublished current copy of the checked source."""
    fd = platform_io.open_private(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    with _source(owner) as source, closing(store.connect_rw(path)) as target:
        counts = store_copy.copy_governed_state(source, target)
    file_sync.sync_path(path)
    return counts


def _replay_candidate(home: Path, path: Path) -> None:
    """Replay after the live source and intent match their capture."""
    with (
        snapshot_io.validated(path) as check,
        closing(store.connect_rw(path)) as target,
    ):
        check()
        tombstones.reapply_tombstones(target, home)
        tombstones.require_current_tombstones(target, home)
        store_copy.validate_source(target)
        target.execute(
            "INSERT INTO event_fts(event_fts,rank) "
            "VALUES('integrity-check',1)"
        )
        target.execute(
            "INSERT INTO knowledge_fts(knowledge_fts) "
            "VALUES('integrity-check')"
        )
        check()
    file_sync.sync_path(path)


def migrate_decisions(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner
) -> Path:
    """Copy exact marked-one state under one protected writer fence."""
    if ctx.data != owner.home:
        raise StepFailedError("cutover_source_mismatch")
    path = store.db_path(ctx.data)
    with q.maintenance_writer(owner):
        original = store_fingerprint.capture(ctx.data)
        with _source(owner):
            pass
        store_fingerprint.verify(ctx.data, original)
        archive = platform_rebuild.retain_upgrade_store(ctx.data, path)
        archived = store_fingerprint.capture_file(archive)
        if (
            archived.digest != original.database.digest
            or archived.size != original.database.size
        ):
            raise StepFailedError("cutover_archive_changed")
        archive_fingerprint = dataclasses.asdict(archived)
        rec["store"] = {
            "phase": "marked_v1_archived",
            "path": str(path),
            "file_identity": list(original.database.identity),
            "archive": str(archive),
            "archive_fingerprint": archive_fingerprint,
        }
        record.save(ctx, rec)
        candidate = ctx.data / (
            store.RECOVERY_PREFIX + "candidate-" + uuid.uuid4().hex
        )
        counts = _candidate(owner, candidate)
        store_fingerprint.verify(ctx.data, original)
        _replay_candidate(ctx.data, candidate)
        store_fingerprint.verify(ctx.data, original)
        _file_matches(archive, archive_fingerprint, "cutover_archive_changed")
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
        with _source(owner):
            pass
        store_fingerprint.verify(ctx.data, original)
        platform_rebuild.publish_replacement(ctx.data, path, candidate)
        with (
            snapshot_io.validated(path) as check,
            closing(store.connect_rw(path)) as conn,
        ):
            check()
            q.verify_current(owner, conn)
            check()
        file_sync.sync_path(path)
        rec["store"]["phase"] = "current_prepared"
        record.save(ctx, rec)
    return path


def prepare_historical(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner
) -> Path:
    """Route strict marked-one sources separately from legacy numeric three."""
    path = store.db_path(ctx.data)
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as check,
        closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        ) as source,
    ):
        check()
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version != 3:
            try:
                version = validate_policy_source(source)
            except StepFailedError as exc:
                raise StepFailedError("cutover_source_mismatch") from exc
        check()
    return (
        migrate_decisions(ctx, rec, owner)
        if version == 1
        else store_migration.prepare(ctx, rec, owner)
    )
