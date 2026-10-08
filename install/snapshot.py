"""Keep the one-way store schema migration reversible across an upgrade.

A new release may migrate ``muninn.sqlite`` to a newer schema the first time
its poller writes, and an older release refuses a newer store. The poller
starts before ``verify`` passes, so a failed upgrade could relink the old
release onto a store it cannot open. Before anything changes, an upgrade whose
release has a higher schema version than the store copies the store with
SQLite's backup API (the old poller may be writing); a failed upgrade puts
that copy back, and a good one deletes it, because it holds transcript text.
"""

from __future__ import annotations

import functools
import os
import re
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from install import lifecycle, snapshot_io
from install.constants import GIT_ENV, PRE_UPGRADE_PREFIX, STORE_FILE
from install.context import Ctx, StepFailedError, dry, job, must, wait
from install.record import Record
from install.release_io import publish
from muninn import file_sync, obs_linux_service, platform_io, store
from muninn.platform_paths import read_selection

SCHEMA_FILE = "muninn/store_schema.py"
_VERSION = re.compile(rb"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", re.MULTILINE)


def release_schema(ctx: Ctx, repo: Path, sha: str) -> int:
    """Read the schema version a commit's store code writes.

    It is read as text from git, not imported, so the installer never runs
    the code it is about to install.

    Args:
        ctx: The run context.
        repo: The source repository.
        sha: The commit being installed.

    Returns:
        The ``SCHEMA_VERSION`` the commit defines.

    Raises:
        StepFailedError: If git cannot show the file or it names no version.
    """
    argv = ["git", "-C", repo, "show", f"{sha}:{SCHEMA_FILE}"]
    found = _VERSION.search(must(ctx, argv, env=GIT_ENV).stdout)
    if found is None:
        raise StepFailedError(f"no SCHEMA_VERSION in {SCHEMA_FILE} at --sha")
    return int(found[1])


@contextmanager
def _readonly(path: Path) -> Generator[sqlite3.Connection]:
    """Hold the validated source while SQLite opens its read-only pathname."""
    with (
        snapshot_io.validated(path),
        closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn,
    ):
        yield conn


def store_version(path: Path) -> int | None:
    """Read a store's ``user_version`` without writing anything.

    Args:
        path: The store file.

    Returns:
        The version, or None when there is no store.

    Raises:
        StepFailedError: If the file exists but cannot be read as a store.
    """
    if not os.path.lexists(path):
        return None
    try:
        with _readonly(path) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise StepFailedError(
            f"cannot read the store's schema version ({type(exc).__name__}); "
            "an upgrade could not come back"
        ) from exc


def _copy(store: Path, tmp: Path, want: int) -> None:
    """Back the store up into ``tmp`` and check the copy.

    Args:
        store: The live store.
        tmp: The new 0600 file to write.
        want: The schema version the copy must carry.

    Raises:
        StepFailedError: If the copy fails its integrity check or carries
            another schema version.
    """
    with _readonly(store) as src:
        os.close(
            platform_io.open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        )
        with closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
            got = dst.execute("PRAGMA user_version").fetchone()[0]
    if check != "ok" or got != want:
        raise StepFailedError("the copy failed its integrity check")
    fd = platform_io.open_private(tmp, os.O_RDWR)
    try:
        file_sync.sync_fd(fd)
    finally:
        os.close(fd)


def _snapshot_store(ctx: Ctx, rec: Record) -> None:
    """Copy the store aside when the new release will migrate its schema.

    Runs before the release is pinned, so it fails closed: nothing has
    changed if it raises.

    Args:
        ctx: The run context.
        rec: The run record; gets ``snapshot`` (name, sizes and versions,
            never content) when a copy is made.

    Raises:
        StepFailedError: If the store cannot be read or copied.
        FileExistsError: If an earlier snapshot pathname already exists.
    """
    store = ctx.data / STORE_FILE
    have, want = store_version(store), rec["schema_to"]
    if have is None or have >= want:
        dry(
            ctx,
            f"store schema is v{have}, the new release needs v{want}: no "
            "pre-upgrade copy needed",
        )
        return
    name = f"{PRE_UPGRADE_PREFIX}{rec['sha'][:12]}"
    final = ctx.data / name
    if dry(
        ctx,
        f"would copy the store (schema v{have}; the new release migrates it "
        f"to v{want}) to {final}, restore it if the upgrade fails and "
        "delete it once the upgrade succeeds",
    ):
        return
    tmp = final.with_name(f"{name}.tmp")
    try:
        if os.path.lexists(final) or os.path.lexists(tmp):
            raise FileExistsError("an earlier private snapshot exists")
        _copy(store, tmp, have)
        publish(tmp, final, replace=False)
    except (OSError, sqlite3.Error, StepFailedError) as exc:
        raise StepFailedError(
            f"could not copy the store ({type(exc).__name__}); nothing was "
            "changed; any private copy was retained"
        ) from exc
    size = final.stat().st_size
    rec["snapshot"] = {"name": name, "from": have, "to": want, "bytes": size}
    ctx.say(f"copied the store (schema v{have}, {size} bytes) to {final}")


def discard(ctx: Ctx, rec: Record) -> bool:
    """Delete the pre-upgrade copy, if the run made one.

    A failure is only a warning: ``muninn doctor`` warns while the file
    exists.

    Args:
        ctx: The run context.
        rec: The run record.

    Returns:
        False when the copy could not be deleted.
    """
    snap = rec.get("snapshot")
    if not snap or ctx.dry_run:
        return True
    path = ctx.data / snap["name"]
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        snap["state"] = "kept"
        ctx.say(
            f"could not delete {path}: {exc}; it holds a copy of your index "
            "and ledger, remove it by hand"
        )
        return False
    snap["state"] = "deleted"
    return True


def _swap(data: Path, snap: Path) -> None:
    """Restore a durable copy only when no residual transaction state exists.

    Retain the original snapshot until durable publication succeeds, so
    uncertainty never consumes the only known good copy.

    Args:
        data: Private live store directory.
        snap: Original private pre-upgrade snapshot.

    Raises:
        OSError: If residual state exists or restoration cannot be verified.
    """
    target = data / STORE_FILE
    if any(
        os.path.lexists(f"{target}{suffix}")
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise OSError("residual SQLite transaction state prevents restoration")
    temporary = snap.with_name(f"{snap.name}.restore.tmp")
    with snapshot_io.validated(target):
        pass
    version = store_version(snap)
    if version is None:
        raise OSError("snapshot is unavailable")
    _copy(snap, temporary, version)
    publish(temporary, target)
    snap.unlink()
    if os.name != "nt":
        file_sync.sync_path(data)


def _running(ctx: Ctx) -> bool:
    """Say whether the launchd job has a live process."""
    found = job(ctx)
    return bool(found and found["pid"])


def undo_store(ctx: Ctx, rec: Record) -> bool:
    """Put the pre-upgrade store back when the new release migrated it.

    Call it after the old release is relinked. A store the new release never
    touched is kept as it is (it has newer ingests than the copy), and the
    copy is deleted.

    Args:
        ctx: The run context.
        rec: The run record.

    Returns:
        True when the job was stopped to restore, so the caller must start
        it again.
    """
    snap = rec.get("snapshot")
    if not snap or not (ctx.data / snap["name"]).exists():
        return False
    try:
        now = store_version(ctx.data / STORE_FILE)
    except StepFailedError:
        now = None  # unreadable: the copy is the only good store
    if now == snap["from"]:
        discard(ctx, rec)
        return False
    stopped = job(ctx) is not None
    dry_tag = "DRY-RUN " if ctx.dry_run else ""
    ctx.say(
        f"{dry_tag}rollback: restore the schema v{snap['from']} store from "
        f"{snap['name']}"
    )
    if ctx.dry_run:
        return stopped
    if stopped:
        lifecycle.stop(ctx)
    with store.writer_lock(ctx.data):
        _swap(ctx.data, ctx.data / snap["name"])
    snap["state"] = "restored"
    return stopped


def start_again(ctx: Ctx) -> None:
    """Load the job again on the relinked release after a restore."""
    if ctx.platform != "darwin":
        if ctx.platform == "win32":
            from_selection = lifecycle_selection(ctx)
            lifecycle.start(ctx, from_selection[1], from_selection[0])
        else:
            current, python, _ = obs_linux_service.selection(ctx.home)
            lifecycle.start(ctx, python, current)
    else:
        must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    wait(ctx, functools.partial(_running, ctx), 30, "job has no live PID")


def lifecycle_selection(ctx: Ctx) -> tuple[Path, Path]:
    """Require the retained native release and recorded interpreter.

    Returns:
        Confined release and recorded interpreter paths.

    Raises:
        StepFailedError: If no safe retained selection exists.
    """
    selected = read_selection(ctx.lib.parent)
    if selected is None:
        raise StepFailedError("retained release selection is unavailable")
    return selected


def snapshot_store(ctx: Ctx, rec: Record) -> None:
    """Serialize the snapshot against other writers without nested leases."""
    if ctx.dry_run:
        _snapshot_store(ctx, rec)
        return
    with store.writer_lock(ctx.data):
        _snapshot_store(ctx, rec)
