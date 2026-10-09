"""Command handlers that write the store: ingest, erase and compact."""

from __future__ import annotations

import dataclasses
import shutil
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from muninn import classify, cli_core, cursor_import, erase, ingest, obs, store
from muninn.cli_core import Env, Out, Record, Result, counts

__all__ = ["compact", "erase_command", "heartbeat", "ingest_command"]


def heartbeat(
    home: Path,
    stats: ingest.PassStats,
    env: Env,
    *,
    clears_error: bool = False,
    **extra: Any,
) -> None:
    """Write ``status.json`` after an ingest pass.

    Only counts and versions go in: the file is read by ``doctor`` and by
    every answer's freshness fields, so it must never hold transcript text.

    Args:
        home: Data directory.
        stats: The finished pass.
        env: Process environment, for the install revision.
        clears_error: Forget ``last_error``; only the poller's own good
            pass says its failure is over.
        **extra: Further fields, such as the poller's pid and pass count.
    """
    fields = dataclasses.asdict(stats)
    # A manual ``ingest`` succeeding says nothing about whether the poller
    # (a different process, perhaps a different release) is working again,
    # so only the poller's pass clears the error it left.
    cleared = {"last_error": None, "last_error_at": None}
    obs.write_status(
        home,
        fields
        | (cleared if clears_error else {})
        | {
            "last_pass_at": time.time(),
            "duration_s": round(stats.duration_s, 3),
            "classifier_version": classify.CLASSIFIER_VERSION,
            "schema_version": store.SCHEMA_VERSION,
            "install_sha": obs.install_sha(env),
        }
        | extra,
    )


def ingest_command(
    a: Namespace, env: Env, home: Path, record: Record
) -> Result:
    """Catch up with transcripts; a full pass also imports Cursor history."""
    cursor_db = (
        cursor_import.default_database(ingest.provider_home(env), env=env)
        if a.full
        else None
    )
    stats = ingest.run_pass(
        home,
        ingest.default_roots(env),
        full=a.full,
        wait_s=cli_core.WRITER_WAIT_S,
    )
    if cursor_db is not None and cursor_db.is_file():
        cursor_import.run(
            home,
            cursor_db,
            cli_core.current_dir(env),
            cli_core.WRITER_WAIT_S,
        )
    heartbeat(home, stats, env)
    out: Out = {"ingest": dataclasses.asdict(stats)}
    record["counts"] = counts(out["ingest"])
    return 0, out


def erase_command(
    a: Namespace, env: Env, home: Path, record: Record
) -> Result:
    """Run ``muninn erase``; without ``--yes`` it is always a dry run."""
    dry = a.dry_run or not a.yes
    out: Out = erase.run_erase(
        home,
        session=a.session,
        event_ref=a.event,
        match=a.match,
        dry_run=dry,
        env=env,
        wait_s=cli_core.WRITER_WAIT_S,
    )
    if dry and not a.dry_run:
        out["note"] = "dry run: add --yes to erase"
    record["counts"] = counts(out)
    return 0, out


def compact(a: Namespace, env: Env, home: Path, record: Record) -> Result:
    """VACUUM the store under the writer lock.

    The poller skips its passes while the lock is held.  VACUUM copies the
    database, so it needs about one database's worth of free disk.

    Args:
        a: Parsed arguments (unused).
        env: Process environment (unused).
        home: Data directory.
        record: Call-log entry; receives the before and after sizes.

    Returns:
        Exit 0 and the sizes plus the new space report.

    Raises:
        StoreUnavailableError: If there is no store yet.
        ValueError: If the disk is too full to hold the copy.
    """
    db = store.db_path(home)
    if not db.exists():
        raise store.StoreUnavailableError("no store")
    with store.writer_lock(home, wait_s=cli_core.WRITER_WAIT_S):
        conn = store.connect_rw(db)
        try:
            before = db.stat().st_size
            free = shutil.disk_usage(home).free
            if free < before:
                raise ValueError(f"need {before} bytes free, have {free}")
            conn.execute("VACUUM")
            space: Any = obs.db_space(conn)
        finally:
            conn.close()
    after = db.stat().st_size
    sizes = {"bytes_before": before, "bytes_after": after}
    record["counts"] = dict(sizes)
    return 0, {"compact": sizes, "db_space": space}
