"""Prepare governed stores without inferring legacy containment authority."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from install import (
    legacy_cutover,
    legacy_recovery,
    legacy_state,
    lifecycle,
    record,
    review_policy,
    snapshot_io,
)
from install.context import Ctx, StepFailedError
from install.record import Record, install_record, save
from install.steps_config import record as record_config
from install.steps_release import start_new
from install.verify import verify
from muninn import (
    cli_core,
    cli_maint,
    cursor_import,
    file_sync,
    ingest,
    platform_io,
    review_requests,
    scope,
    store,
)
from muninn import review_quiescence as q
from muninn import review_quiescence_state as state
from muninn.review_models import ReviewPolicy
from muninn.review_progress import checked_policy, digest
from muninn.store_copy import copy_governed_state as copy_governed_state


def _same_home(home: Path, identity: tuple[int, int]) -> None:
    """Refuse replacement of the directory exclusively created by this run."""
    info = home.lstat()
    if home.is_symlink() or (info.st_dev, info.st_ino) != identity:
        raise StepFailedError("fresh_home_changed")


def _create_home(ctx: Ctx) -> tuple[int, int]:
    """Exclusively create and identify the new private data directory."""
    ctx.data.parent.mkdir(parents=True, exist_ok=True)
    try:
        ctx.data.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StepFailedError("fresh_home_exists") from exc
    info = ctx.data.lstat()
    identity = info.st_dev, info.st_ino
    if not platform_io.is_private(ctx.data, directory=True):
        raise StepFailedError("fresh_home_not_private")
    return identity


def _fresh(
    ctx: Ctx, rec: Record, owner: q.MaintenanceOwner, identity: tuple[int, int]
) -> Path:
    """Initialize the exclusively created home beneath its retained owner."""
    path = store.db_path(ctx.data)
    with q.maintenance_writer(owner):
        _same_home(ctx.data, identity)
        if any(
            state.inspect(ctx.data, name)[1] is not None
            for name in (state.WITNESS, state.BASELINE)
        ):
            raise state.QuiescenceRefusedError("quiescence_unverified")
        if os.path.lexists(path):
            raise StepFailedError("fresh_home_changed")
        with closing(store.connect_rw(path)) as conn:
            store.assert_store_format(conn)
        _same_home(ctx.data, identity)
        file_sync.sync_path(path)
        state.seed_new(ctx.data, owner.fd)
        _same_home(ctx.data, identity)
        rec["store"] = {
            "phase": "fresh_initialized",
            "path": str(path),
            "home_identity": list(identity),
            "file_identity": [path.stat().st_dev, path.stat().st_ino],
        }
        record.save(ctx, rec)
    return path


def _ingest_fresh(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    identity: tuple[int, int],
) -> None:
    """Finish all provider work and record actual counts before activation."""
    with snapshot_io.validated(path) as revalidate:
        stats = ingest_transcripts(ctx, owner)
        revalidate()
        database = cursor_import.default_database(ctx.home, env={})
        prepared = (
            cursor_import.prepare(database, cli_core.current_dir({}))
            if database is not None and database.is_file()
            else None
        )
        with q.maintenance_writer(owner):
            _same_home(ctx.data, identity)
            revalidate()
            with closing(store.connect_rw(path)) as conn:
                revalidate()
                q.verify_current(owner, conn)
                if prepared is not None:
                    cursor_import.apply(conn, ctx.data, prepared)
                counts = dict(
                    conn.execute(
                        "SELECT s.provider,count(*) FROM event e"
                        " JOIN source s ON s.id=e.source_id"
                        " GROUP BY s.provider"
                    )
                )
                revalidate()
            file_sync.sync_path(path)
            revalidate()
            _same_home(ctx.data, identity)
            cli_maint.heartbeat(
                ctx.data, stats, {"MUNINN_INSTALL_SHA": rec["sha"]}
            )
            rec["ingest"] = dataclasses.asdict(stats)
            rec["events_by_provider"] = counts
            rec["store"]["phase"] = "fresh_prepared"
            record.save(ctx, rec)


def _fence_current(conn: sqlite3.Connection) -> sqlite3.Row:
    """Commit the stopped admission and fence all abandoned attempts."""
    control = conn.execute("SELECT * FROM review_control").fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE review_control SET admission=0,generation=generation+1"
        )
        review_requests.recover_attempts(
            conn, control["generation"] + 1, time.time()
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return control


def ingest_transcripts(
    ctx: Ctx, owner: q.MaintenanceOwner
) -> ingest.PassStats:
    """Replan full transcript passes beneath one retained installer owner.

    Args:
        ctx: Installer paths with explicitly selected provider homes.
        owner: Live maintenance owner whose acquisition deadline is shared.

    Returns:
        Completed invocation work totals, including repeated full passes.

    Raises:
        StepFailedError: If any ordinary attempt failed or was unreadable.
        OSError: If the retained store pathname changes identity.
    """
    roots = ingest.default_roots(
        {
            "HOME": str(ctx.home),
            "USERPROFILE": str(ctx.home),
            "CODEX_HOME": str(ctx.codex_home),
            "CLAUDE_CONFIG_DIR": str(ctx.settings.parent),
        }
    )
    path = store.db_path(ctx.data)
    started, total = time.monotonic(), ingest.PassStats()
    facts: scope.CommitFacts = {}
    revalidate: Callable[[], None] | None = None
    with ExitStack() as identities:
        while True:
            try:
                with q.maintenance_writer(owner):
                    if revalidate is None:
                        revalidate = identities.enter_context(
                            snapshot_io.validated(path)
                        )
                    revalidate()
                    with closing(store.connect_rw(path)) as conn:
                        revalidate()
                        q.verify_current(owner, conn)
                        stats = ingest.ingest(
                            conn, roots, full=True, commit_facts=facts
                        )
                        revalidate()
                    total.accumulate(stats)
                    if total.failed or total.errors or total.unreadable_files:
                        raise StepFailedError("fresh_ingest_incomplete")
                    file_sync.sync_path(path)
                    revalidate()
            except ingest.IngestPreparationRequiredError as pending:
                total.accumulate(pending.stats)
                scope.prepare_commit(pending.lookup, facts)
            else:
                total.duration_s = time.monotonic() - started
                return total


def _restore_admission(conn: sqlite3.Connection, control: sqlite3.Row) -> None:
    """Validate current data and restore only the prior permitted policy."""
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None or (
        [row[0] for row in conn.execute("PRAGMA quick_check")] != ["ok"]
    ):
        raise StepFailedError("current_store_invalid")
    if not control["admission"]:
        return
    try:
        policy = checked_policy(
            ReviewPolicy.model_validate_json(control["policy_json"])
        )
        if control["policy_digest"] != digest(policy):
            raise ValueError("policy_digest_mismatch")
    except ValueError:
        raise ValueError("review_policy_required") from None
    conn.execute("UPDATE review_control SET admission=1")


def _current(ctx: Ctx, rec: Record, owner: q.MaintenanceOwner) -> Path:
    """Maintain a verified current file without replacing its pathname."""
    path = store.db_path(ctx.data)
    with (
        q.maintenance_writer(owner),
        snapshot_io.validated(path) as revalidate,
    ):
        try:
            with closing(store.connect_ro(path)) as conn:
                revalidate()
                store.assert_store_format(conn)
        except store.StoreUnavailableError as exc:
            raise StepFailedError("cutover_source_mismatch") from exc
        rec["store"] = {
            "phase": "current_mutation_possible",
            "path": str(path),
            "file_identity": [path.stat().st_dev, path.stat().st_ino],
        }
        record.save(ctx, rec)
        with closing(store.connect_rw(path)) as conn:
            revalidate()
            q.verify_current(owner, conn)
            control = _fence_current(conn)
            _restore_admission(conn, control)
            revalidate()
        file_sync.sync_path(path)
        revalidate()
        rec["store"]["phase"] = "current_prepared"
        record.save(ctx, rec)
    return path


@contextmanager
def _prepared(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck | None = None
) -> Generator[tuple[Path, q.MaintenanceOwner | None]]:
    """Retain maintenance ownership through the caller's activation work.

    The writer region ends before yielding. The caller releases this scope
    before starting a service or entering native rollback cleanup.

    Args:
        ctx: Installer paths and explicit fresh or upgrade mode.
        rec: Durable installer record without transient lock objects.
        check: Held operation validation for installer continuation.

    Yields:
        The prepared path and retained owner, or None in a dry run.

    Raises:
        StepFailedError: If the prepared store changed identity.
    """
    if ctx.dry_run:
        ctx.say("would prepare the governed store before consumer activation")
        yield store.db_path(ctx.data), None
        return
    identity = _create_home(ctx) if ctx.fresh else None
    with q.maintenance_owner(
        ctx.data, wait_s=cli_core.WRITER_WAIT_S, status=q.EraseStatus()
    ) as owner:
        if identity is not None:
            path = _fresh(ctx, rec, owner, identity)
            _ingest_fresh(ctx, rec, owner, path, identity)
        elif "legacy" in rec:
            if check is None:
                raise StepFailedError("install_operation_required")
            prepare = (
                legacy_cutover.prepare
                if rec["legacy"]["publication"] == "not_started"
                else legacy_recovery.prepare
            )
            path = prepare(ctx, rec, owner, check)
        else:
            path = _current(ctx, rec, owner)
        rec["policy_store_identity"] = list(
            rec["legacy"]["publication_binding"]["candidate"]["identity"]
            if "legacy" in rec
            else rec["store"]["file_identity"]
        )
        record.save(ctx, rec)
        yield path, owner


@contextmanager
def prepared_store(ctx: Ctx, rec: Record) -> Generator[Path]:
    """Prepare a store while retaining its owner through the caller scope."""
    with _prepared(ctx, rec) as (path, _owner):
        yield path


@contextmanager
def prepared_install_store(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck
) -> Generator[Path]:
    """Activate the first explicit policy only after normal installer exit."""

    def activation_check() -> tuple[int, int]:
        identity = check()
        if "legacy" in rec:
            legacy_recovery.verify_configuration(ctx, rec)
        check()
        return identity

    with _prepared(ctx, rec, check) as (path, owner):
        try:
            yield path
        except BaseException:
            raise
        else:
            if owner is not None:
                review_policy.ready(ctx, rec)
                review_policy.activate(ctx, rec, owner, path, activation_check)
                if "legacy" in rec:
                    legacy_recovery.expose(ctx, rec, check)


def prepare_store(ctx: Ctx, rec: Record) -> Path:
    """Prepare a store only through a supported, proven maintenance route.

    Args:
        ctx: Installer context with isolated paths and explicit install mode.
        rec: Existing durable installer record, updated before activation.

    Returns:
        The initialized marked store pathname.

    Raises:
        StepFailedError: If positive fresh provenance cannot be established
            or the source has no implemented authorized preparation route.
    """
    with prepared_store(ctx, rec) as path:
        return path


def install_legacy(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck
) -> Record:
    """Continue the retained operation without rollback across exposure."""
    record_config(ctx, rec)
    try:
        with prepared_install_store(ctx, rec, check):
            check()
            legacy_recovery.configure(ctx, rec)
            check()
        check()
        rec["legacy"]["phase"] = "starting"
        save(ctx, rec)
        check()
        start_new(ctx, rec)
        verify(ctx, rec)
        rec["legacy"]["phase"] = "complete"
        rec.pop("legacy_outcome", None)
        rec.pop("recovery_required", None)
        save(ctx, rec)
        install_record(ctx, rec, "ok")
        return rec
    except Exception:
        rec["recovery_required"] = True
        rec["legacy_outcome"] = "recovery_required"
        try:
            save(ctx, rec)
            install_record(ctx, rec, "recovery_required")
        except Exception:
            ctx.say("recovery_required: durable outcome could not be updated")
        raise


def resume_policy(
    ctx: Ctx, rec: Record, check: legacy_state.OperationCheck
) -> Record:
    """Finish retained first activation without resetting its control state."""
    review_policy.verify_ready(ctx, rec)
    check()
    lifecycle.stop(ctx)
    with q.maintenance_owner(
        ctx.data, wait_s=cli_core.WRITER_WAIT_S, status=q.EraseStatus()
    ) as owner:
        review_policy.activate(ctx, rec, owner, store.db_path(ctx.data), check)
    check()
    start_new(ctx, rec)
    verify(ctx, rec)
    rec["install_complete"] = True
    rec.pop("recovery_required", None)
    save(ctx, rec)
    install_record(ctx, rec, "ok")
    return rec
