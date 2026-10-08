"""Real managed Linux writers commit before stop and restart after a crash."""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from install import lifecycle, snapshot_io
from install.context import Ctx, must, wait
from install.release_io import write_private
from muninn import obs, obs_linux_service, obs_status, platform_io, store
from muninn.obs_service import service_status
from tests.ingest_support import TID, line, primary, rollout
from tests.native_diagnostics import write


def journal_ready(data: Path) -> bool:
    """Require real rollback-journal bytes rather than a pathname alone."""
    journal = data / "muninn.sqlite-journal"
    try:
        return journal.is_file() and journal.stat().st_size >= 512
    except FileNotFoundError:
        return False


def committed_rows(conn: sqlite3.Connection, session: str) -> int:
    """Count committed synthetic events belonging to exactly one session."""
    return int(
        conn.execute(
            "SELECT count(*) FROM event JOIN source "
            "ON event.source_id=source.id WHERE source.session_root=?",
            (session,),
        ).fetchone()[0]
    )


def _native(ctx: Ctx) -> None:
    """Refuse synthetic process operations outside the Linux user manager."""
    if sys.platform != "linux" or ctx.platform != "linux":
        raise OSError("native_linux_required")


def _verified(ctx: Ctx) -> int:
    """Bind the actual writer to its owned unit and loaded release SHA."""
    healthy, pid = service_status(ctx.data, {"HOME": str(ctx.home)}, obs.run)
    assert healthy is True and isinstance(pid, int) and pid > 0
    assert obs_status.read_status(ctx.data)["writer_install_sha"] == (
        ctx.release.resolve().name
    )
    return pid


def busy_stop(ctx: Ctx) -> None:
    """Block an actual managed commit, request stop, then allow completion."""
    _native(ctx)
    write("lifecycle", "busy_stop")
    source = ctx.codex_home / "sessions" / rollout(TID)
    platform_io.ensure_private_dir(source.parent)
    write_private(source, b"".join(line(record) for record in primary(TID)))
    database = store.db_path(ctx.data)
    with (
        snapshot_io.validated(database),
        closing(store.connect_ro(database)) as reader,
    ):
        reader.execute("BEGIN")
        assert committed_rows(reader, TID) == 0
        lifecycle.start(ctx, (ctx.lib / "python").resolve(), ctx.release)
        wait(
            ctx,
            lambda: journal_ready(ctx.data),
            20,
            "managed transaction absent",
        )
        wait(
            ctx,
            lambda: obs_status.read_status(ctx.data).get("pid")
            == (found["pid"] if (found := lifecycle.job(ctx)) else None),
            2,
            "writer heartbeat absent",
        )
        writer = _verified(ctx)
        identity = obs_linux_service.process_identity(writer)
        assert obs_status.read_status(ctx.data)["pid"] == writer
        with ThreadPoolExecutor(max_workers=1) as executor:
            stopping = executor.submit(lifecycle.stop, ctx)
            try:
                wait(
                    ctx,
                    lambda: must(
                        ctx,
                        [
                            "systemctl",
                            "--user",
                            "show",
                            "--property=ActiveState",
                            "--value",
                            ctx.target,
                        ],
                        quiet=True,
                    ).stdout.strip()
                    == b"deactivating",
                    2,
                    "managed stop intent absent before commit",
                )
                assert journal_ready(ctx.data)
                assert obs_linux_service.process_identity(writer) == identity
            finally:
                reader.rollback()
            stopping.result(timeout=35)
        assert committed_rows(reader, TID) == 2
    assert not journal_ready(ctx.data)
    assert not ((found := lifecycle.job(ctx)) and found["pid"])
    time.sleep(6)
    assert not ((found := lifecycle.job(ctx)) and found["pid"])


def _completed(ctx: Ctx, started: float) -> bool:
    """Require a finished pass from this start before crash injection."""
    value = obs_status.read_status(ctx.data).get("last_pass_at")
    return isinstance(value, (int, float)) and value >= started


def crash_restart(ctx: Ctx, started: float) -> None:
    """Crash a verified idle synthetic writer and prove service recovery."""
    _native(ctx)
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    write("lifecycle", "crash_restart")
    writer = _verified(ctx)
    wait(ctx, lambda: _completed(ctx, started), 20, "idle managed pass absent")
    identity = obs_linux_service.process_identity(writer)
    assert not journal_ready(ctx.data)
    descriptor = os.pidfd_open(writer)
    try:
        assert obs_linux_service.process_identity(writer) == identity
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)
    wait(
        ctx,
        lambda: bool(
            (found := lifecycle.job(ctx))
            and found["pid"]
            and found["pid"] != writer
        ),
        20,
        "managed crash did not restart",
    )
    wait(
        ctx,
        lambda: obs_status.read_status(ctx.data).get("pid") != writer,
        20,
        "replacement heartbeat absent",
    )
    replacement = _verified(ctx)
    assert replacement != writer
    lifecycle.stop(ctx)
    time.sleep(6)
    assert not ((found := lifecycle.job(ctx)) and found["pid"])
