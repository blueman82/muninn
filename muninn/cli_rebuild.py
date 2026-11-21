"""Rebuild current data with retained recovery copies and no restore."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sqlite3
import time
from argparse import Namespace
from collections.abc import Generator
from pathlib import Path

from muninn import (
    cli_core,
    ingest,
    platform_io,
    platform_rebuild,
    query,
    review_requests,
    scope,
    store,
    store_copy,
    tombstone_key,
    tombstones,
)
from muninn import store_fingerprint as fingerprint
from muninn.cli_core import Env, Out, Record, Result

__all__ = ["REBUILD", "rebuild"]
REBUILD = "muninn.sqlite.rebuild"


@dataclasses.dataclass
class _Status:
    """Keep conservative milestones without inferring restoration authority."""

    mutation_possible: bool = False
    publication_possible: bool = False
    archive: str | None = None


@dataclasses.dataclass(frozen=True)
class _Prepared:
    """Retain original observations and policy across writer gaps."""

    original: fingerprint.Fingerprint
    archive: fingerprint.FileFingerprint
    candidate: fingerprint.FileFingerprint
    copied: dict[str, int]


def _same_file(path: Path, original: fingerprint.FileFingerprint) -> None:
    """Refuse changed files without refreshing their original baseline."""
    if fingerprint.capture_file(path) != original:
        raise fingerprint.SourceChangedError


@contextlib.contextmanager
def _connection(
    path: Path,
    original: fingerprint.FileFingerprint,
    *,
    readonly: bool = False,
) -> Generator[sqlite3.Connection]:
    """Open inside the caller's writer region and preserve primary errors."""
    conn = store.connect_ro(path) if readonly else store.connect_rw(path)
    try:
        if fingerprint.capture_file(path).identity != original.identity:
            raise fingerprint.SourceChangedError
        yield conn
        if fingerprint.capture_file(path).identity != original.identity:
            raise fingerprint.SourceChangedError
    except BaseException:
        with contextlib.suppress(BaseException):
            conn.close()
        raise
    else:
        conn.close()


def _fence(conn: sqlite3.Connection) -> None:
    """Fence running attempts, retaining unknown charges."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        review_requests.recover_attempts(conn, time.time())
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(BaseException):
            conn.execute("ROLLBACK")
        raise


def _prepare(home: Path, status: _Status) -> _Prepared:
    """Fence and retain the live source before exclusively building a copy."""
    db, new = store.db_path(home), home / REBUILD
    with store.writer_lock(home, wait_s=cli_core.WRITER_WAIT_S):
        if any(
            os.path.lexists(str(new) + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")
        ):
            status.publication_possible = True
            raise ValueError("rebuild_candidate_exists")
        before = fingerprint.capture(home)
        with _connection(db, before.database, readonly=True) as source:
            if (
                os.path.lexists(home / tombstone_key.KEY_FILE)
                or tombstone_key.table_has_keyed(source)
                or tombstone_key.log_has_keyed(home)
            ):
                tombstone_key.load_key(home, create=False)
        fingerprint.verify(home, before)
        with _connection(db, before.database) as conn:
            status.mutation_possible = True
            tombstones.reapply_tombstones(conn, home)
            _fence(conn)
        original = fingerprint.capture(home)
        if (original.home_identity, original.database.identity) != (
            before.home_identity,
            before.database.identity,
        ):
            raise fingerprint.SourceChangedError
        archive = platform_rebuild.retain_store(home, db)
        status.archive = archive.name
        retained = fingerprint.capture_file(archive)
        if retained.digest != original.database.digest:
            raise fingerprint.SourceChangedError
        fingerprint.verify(home, original)
        fd = platform_io.open_private(new, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
        empty = fingerprint.capture_file(new)
        fingerprint.verify(home, original)
        with (
            _connection(db, original.database, readonly=True) as source,
            _connection(new, empty) as target,
        ):
            copied = store_copy.copy_governed_state(source, target)
        fingerprint.verify(home, original)
        return _Prepared(
            original, retained, fingerprint.capture_file(new), copied
        )


def _validate(conn: sqlite3.Connection) -> None:
    """Validate the rebuilt content."""
    store.assert_store_format(conn)
    store_copy.rebuild_indexes(conn)
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("rebuild_foreign_keys")
    if [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("quick_check_failed")
    conn.execute(
        "INSERT INTO event_fts(event_fts,rank) VALUES('integrity-check',1)"
    )
    conn.execute(
        "INSERT INTO knowledge_fts(knowledge_fts) VALUES('integrity-check')"
    )


def _verify_after_own_open(
    home: Path, original: fingerprint.Fingerprint
) -> None:
    """Check the source after our RW open, within the same final writer.

    The opener may change DB ctime when normalizing private permissions.
    Only that timestamp may differ after a strict pre-open comparison; the
    original remains immutable and every across-gap comparison stays strict.

    Raises:
        SourceChangedError: If any other original source observation differs.
    """
    actual = fingerprint.capture(home)
    normalized = dataclasses.replace(
        actual,
        database=dataclasses.replace(
            actual.database, ctime_ns=original.database.ctime_ns
        ),
    )
    if normalized != original:
        raise fingerprint.SourceChangedError


def _finish(
    home: Path,
    prepared: _Prepared,
    candidate: fingerprint.FileFingerprint,
    status: _Status,
) -> int:
    """Replay and validate under the final uninterrupted writer barrier."""
    db, new = store.db_path(home), home / REBUILD
    fingerprint.verify(home, prepared.original)
    # Even our own replay can invalidate the copied view. Refuse conservatively
    # rather than publish from a different source than the frozen original.
    _verify_after_own_open(home, prepared.original)
    _same_file(new, candidate)
    with _connection(new, candidate) as target:
        reapplied = tombstones.reapply_tombstones(target, home)
        tombstones.require_current_tombstones(target, home)
        _validate(target)
    validated = fingerprint.capture_file(new)
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(str(new) + suffix):
            raise ValueError("rebuild_candidate_unsettled")
    assert status.archive is not None
    _same_file(home / status.archive, prepared.archive)
    _verify_after_own_open(home, prepared.original)
    _same_file(new, validated)
    status.publication_possible = True
    platform_rebuild.publish_replacement(home, db, new)
    return reapplied


@query.guarded
def _run(env: Env, home: Path, status: _Status) -> Out:
    """Retain one owner and deadline across complete ingest attempts."""
    started, total = time.monotonic(), ingest.PassStats()
    facts: scope.CommitFacts = {}
    prepared = _prepare(home, status)
    candidate = prepared.candidate
    while True:
        pending: ingest.IngestPreparationRequiredError | None = None
        with store.writer_lock(home, wait_s=cli_core.WRITER_WAIT_S):
            fingerprint.verify(home, prepared.original)
            new = home / REBUILD
            _same_file(new, candidate)
            with _connection(new, candidate) as target:
                try:
                    stats = ingest.ingest(
                        target,
                        ingest.default_roots(env),
                        full=True,
                        commit_facts=facts,
                    )
                except ingest.IngestPreparationRequiredError as exc:
                    pending = exc
                    total.accumulate(exc.stats)
                else:
                    total.accumulate(stats)
            candidate = fingerprint.capture_file(new)
            if pending is None:
                if total.failed or total.errors or total.unreadable_files:
                    raise ValueError("rebuild_ingest_incomplete")
                reapplied = _finish(home, prepared, candidate, status)
                break
        assert pending is not None
        scope.prepare_commit(pending.lookup, facts)
    total.duration_s = time.monotonic() - started
    return {
        "rebuilt": True,
        "old_readable": True,
        "old_kept_as": status.archive,
        "warning": "a recovery archive remains; automatic restore is refused",
        "recovery_required": False,
        "publication_possible": True,
        "copied": prepared.copied,
        "reapplied_tombstones": reapplied,
        "ingest": dataclasses.asdict(total),
    }


def rebuild(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Publish a complete current-format candidate without automatic recovery.

    Args:
        a: Parsed arguments, currently unused.
        env: Explicit provider roots and environment.
        home: Existing private current data directory.
        record: Content-free call-log counters, never durable authority.

    Returns:
        Standard CLI exit code and safe counts or conservative failure state.
        All artifacts remain as observed after any failure; no restore,
        candidate promotion, retry or inferred admission reenable is attempted.
    """
    status = _Status()
    try:
        out = _run(env, home, status)
    except store.BusyError:
        code, out = 3, {"error": "busy"}
    except tombstone_key.TombstoneKeyError:
        code, out = 4, {"error": "tombstone_key"}
    except store.HotJournalError:
        code, out = 4, {"error": "hot_journal"}
    except (store.StoreUnavailableError, OSError):
        code, out = 4, {
            "error": (
                "replace_failed"
                if status.publication_possible
                else "store_unavailable"
            )
        }
    except ValueError as exc:
        reason = str(exc)
        allowed = {
            "source_changed",
            "rebuild_candidate_exists",
            "rebuild_candidate_unsettled",
            "rebuild_ingest_incomplete",
            "rebuild_foreign_keys",
            "quick_check_failed",
            "cutover_source_mismatch",
            "cutover_foreign_keys",
            "cutover_integrity",
            "cutover_checksum_mismatch",
            "erasure_pending",
        }
        code, out = 2, {
            "error": "refused",
            "reason": reason if reason in allowed else "rebuild_refused",
        }
    else:
        record["counts"] = out["copied"] | {
            "reapplied": out["reapplied_tombstones"]
        }
        return 0, out
    out |= {
        "recovery_required": status.mutation_possible
        or status.publication_possible,
        "publication_possible": status.publication_possible,
        "old_kept_as": status.archive,
    }
    return code, out
