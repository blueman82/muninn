"""Retain private store archives without restoring over current data."""

from __future__ import annotations

import functools
import os
import re
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from install import lifecycle, snapshot_io
from install.constants import GIT_ENV
from install.context import Ctx, StepFailedError, job, must, wait
from install.cutover import prepare_store
from install.record import Record
from muninn import obs_linux_service
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
        snapshot_io.validated(path) as revalidate,
        closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn,
    ):
        revalidate()
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


def discard(ctx: Ctx, rec: Record) -> bool:
    """Retain every recorded recoverable archive after a successful upgrade.

    Args:
        ctx: Installer context, including dry-run mode.
        rec: Existing run record naming any retained source archive.

    Returns:
        True; retention does not remove the recorded file.
    """
    snap = rec.get("snapshot")
    if snap and not ctx.dry_run:
        snap["state"] = "retained"
    return True


def _running(ctx: Ctx) -> bool:
    """Say whether the launchd job has a live process."""
    found = job(ctx)
    return bool(found and found["pid"])


def undo_store(ctx: Ctx, rec: Record) -> bool:
    """Refuse blind restoration of an archive over potentially newer state.

    Args:
        ctx: Installer context identifying the current data directory.
        rec: Existing rollback record naming a recoverable archive.

    Returns:
        False if no archive requires a recovery decision.

    Raises:
        StepFailedError: If a retained archive needs explicit review.
    """
    snap = rec.get("snapshot")
    if snap and (ctx.data / snap["name"]).exists():
        raise StepFailedError("archive_restore_requires_review")
    return False


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
    """Prepare the governed store without numeric-version migration guesses."""
    prepare_store(ctx, rec)
