"""Real managed Linux writers commit before stop and refuse unsafe restart."""

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
from muninn import (
    obs,
    obs_linux_service,
    obs_status,
    platform_io,
    review_quiescence_state,
    store,
)
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
    assert healthy is True and isinstance(pid, int) and pid > 0, (healthy, pid)
    assert obs_status.read_status(ctx.data)["writer_install_sha"] == (
        ctx.release.resolve().name
    )
    return pid


def busy_stop(ctx: Ctx) -> None:
    """Block an actual managed commit, request stop, then allow completion."""
    _native(ctx)
    write("lifecycle", "busy_stop")
    database = store.db_path(ctx.data)
    started = time.time()
    lifecycle.start(ctx, (ctx.lib / "python").readlink(), ctx.release)
    wait(ctx, lambda: _completed(ctx, started), 20, "idle managed pass absent")
    with (
        snapshot_io.validated(database),
        closing(store.connect_ro(database)) as reader,
    ):
        reader.execute("BEGIN")
        assert committed_rows(reader, TID) == 0
        source = ctx.codex_home / "sessions" / rollout(TID)
        platform_io.ensure_private_dir(source.parent)
        write_private(
            source, b"".join(line(record) for record in primary(TID))
        )
        wait(
            ctx,
            lambda: journal_ready(ctx.data),
            75,
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


def replacement_refused(raw: bytes, writer: int) -> bool:
    """Require a distinct replacement whose nonzero exit systemd observed."""
    fields = dict(
        line.split("=", 1)
        for line in raw.decode("ascii").splitlines()
        if "=" in line
    )
    try:
        return (
            int(fields["NRestarts"]) >= 1
            and int(fields["ExecMainPID"]) > 0
            and int(fields["ExecMainPID"]) != writer
            and int(fields["ExecMainCode"]) == 1
            and int(fields["ExecMainStatus"]) > 0
            and int(fields["ExecMainExitTimestampMonotonic"]) > 0
            and int(fields["MainPID"]) == 0
            and fields["Result"] == "exit-code"
        )
    except (KeyError, ValueError):
        return False


def crash_refusal(ctx: Ctx) -> None:
    """Crash an idle writer and prove the next owner cannot bypass repair."""
    _native(ctx)
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    write("lifecycle", "crash_refusal")
    selected, python, _ = obs_linux_service.selection(ctx.home)
    started = time.time()
    lifecycle.start(ctx, python, selected)
    wait(ctx, lambda: _completed(ctx, started), 20, "idle managed pass absent")
    healthy, writer = obs_linux_service.inspect(
        ctx.data, {"HOME": str(ctx.home)}, obs.run
    )
    assert healthy is True and isinstance(writer, int) and writer > 0
    identity = obs_linux_service.process_identity(writer)
    assert not journal_ready(ctx.data)
    before = review_quiescence_state.parse(
        review_quiescence_state.inspect(
            ctx.data, review_quiescence_state.WITNESS
        )[0],
        review_quiescence_state.Witness,
    )
    assert before is not None and before.state == "active"
    assert before.owner is not None and before.owner.pid == writer
    descriptor = os.pidfd_open(writer)
    try:
        assert obs_linux_service.process_identity(writer) == identity
        crashed_at = time.time()
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)
    try:
        wait(
            ctx,
            lambda: replacement_refused(
                must(
                    ctx,
                    [
                        "systemctl",
                        "--user",
                        "show",
                        "--all",
                        "--property=NRestarts,ExecMainPID,ExecMainCode,"
                        "ExecMainStatus,ExecMainExitTimestampMonotonic,"
                        "MainPID,Result",
                        ctx.target,
                    ],
                    quiet=True,
                ).stdout,
                writer,
            ),
            20,
            "managed replacement did not refuse",
        )
        status = obs_status.read_status(ctx.data)
        assert status.get("pid") == writer
        assert status.get("last_error") == "QuiescenceRefusedError"
        error_at = status.get("last_error_at")
        assert isinstance(error_at, (int, float)) and error_at >= crashed_at
        after = review_quiescence_state.parse(
            review_quiescence_state.inspect(
                ctx.data, review_quiescence_state.WITNESS
            )[0],
            review_quiescence_state.Witness,
        )
        assert after == before
    finally:
        must(ctx, ["systemctl", "--user", "stop", ctx.target], quiet=True)
    wait(
        ctx,
        lambda: obs_linux_service.unit_stopped(obs.run),
        20,
        "failed replacement did not stop",
    )
    assert obs_status.read_status(ctx.data).get("pid") == writer
