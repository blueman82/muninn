"""Actual managed current-store recovery across synthetic releases."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from install import installer, lifecycle, snapshot_io
from install.context import Ctx, StepFailedError, wait
from install.runtime import runtime_python
from muninn import (
    obs,
    obs_linux_service,
    obs_status,
    platform_io,
    store,
    tombstone_key,
)
from tests import lifecycle_native_doctor
from tests.native_diagnostics import write

_ANCHOR = (
    "        if empty is not None or version != 0:\n"
    "            assert_store_format(conn)\n"
)
_WRITE = (
    "            conn.execute(\n"
    '                "CREATE TABLE IF NOT EXISTS native_upgrade_marker("\n'
    '                "value INTEGER PRIMARY KEY CHECK(value=1))"\n'
    "            )\n"
    "            conn.execute(\n"
    '                "INSERT OR IGNORE INTO native_upgrade_marker VALUES(1)"\n'
    "            )\n"
)


def mutation_source(source: str) -> str:
    """Add a fixture write inside the existing current-store transaction."""
    if source.count(_ANCHOR) != 1:
        raise ValueError("fixture_source_changed")
    return source.replace(_ANCHOR, _ANCHOR + _WRITE)


def fixture_release(parent: Path, root: Path) -> tuple[Path, str]:
    """Create a private release with a transactional current-store write."""
    repo = parent / "rollback-repo"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(root), str(repo)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    source = repo / "muninn/store.py"
    source.write_text(mutation_source(source.read_text()))
    for argv in (
        ["config", "user.name", "Muninn Native Fixture"],
        ["config", "user.email", "native-fixture@example.invalid"],
        ["add", "muninn/store.py"],
        ["commit", "-m", "Synthetic transactional native write"],
    ):
        subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, check=True
        )
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    assert not subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"]
    )
    return repo, sha


def alternate_interpreter(source: Path) -> Path:
    """Copy an executable exclusively inside its already private CI prefix."""
    if not platform_io.is_private(source.parent, directory=True):
        raise OSError("private_interpreter_prefix_required")
    target = source.parent / "python-muninn-native-upgrade"
    if os.path.lexists(target):
        raise FileExistsError("unknown_alternate_interpreter_retained")
    with platform_io.open_regular(source, root=source.parent) as handle:
        platform_io.assert_private_fd(handle.fileno())
        data = handle.read()
    descriptor = platform_io.open_private(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    target.chmod(0o700)
    assert target.read_bytes() == data
    return target


def database_state(data: Path) -> tuple[int, int, int, bool]:
    """Read private committed schema/counts without provider content."""
    database = data / "muninn.sqlite"
    with (
        snapshot_io.validated(database) as revalidate,
        closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        ) as conn,
    ):
        revalidate()
        store.assert_store_format(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        sources = int(
            conn.execute("SELECT count(*) FROM source").fetchone()[0]
        )
        events = int(conn.execute("SELECT count(*) FROM event").fetchone()[0])
        marker = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE name='native_upgrade_marker'"
            ).fetchone()
        )
        if marker:
            assert conn.execute(
                "SELECT value FROM native_upgrade_marker"
            ).fetchall() == [(1,)]
    return version, sources, events, marker


def observe_writer(
    ctx: Ctx,
    new_sha: str,
    alternate: Path,
    old_pid: int,
    before: tuple[int, int, int, bool],
) -> int:
    """Require committed rows and the distinct executable/root identity."""
    state = database_state(ctx.data)
    assert state == (before[0], before[1], before[2], True)
    selected, executable, sha = obs_linux_service.selection(ctx.home)
    assert sha == new_sha
    assert executable == runtime_python(ctx.lib / new_sha, ctx.platform)
    assert executable.resolve(strict=True) == alternate
    pid = obs_status.read_status(ctx.data)["pid"]
    assert isinstance(pid, int) and pid != old_pid
    _, actual_exe, _ = obs_linux_service.process_identity(pid)
    assert actual_exe == alternate and selected.resolve().name == new_sha
    assert obs_status.read_status(ctx.data)["writer_install_sha"] == new_sha
    return pid


def wait_healthy(ctx: Ctx, previous: object) -> int:
    """Require an inspected healthy replacement rather than stale status."""

    def ready() -> bool:
        try:
            healthy, pid = obs_linux_service.inspect(
                ctx.data, {"HOME": str(ctx.home)}, obs.run
            )
        except (OSError, ValueError):
            return False
        return (
            healthy is True
            and type(pid) is int
            and pid > 0
            and pid != previous
        )

    wait(ctx, ready, 20, "healthy replacement writer absent")
    healthy, pid = obs_linux_service.inspect(
        ctx.data, {"HOME": str(ctx.home)}, obs.run
    )
    assert healthy is True and type(pid) is int and pid > 0 and pid != previous
    return pid


def rollback(ctx: Ctx, root: Path) -> Path:
    """Retain exposed current data and runtime after failed verification.

    Returns:
        The synthetic source repository matching the retained selection.
    """
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    write("lifecycle", "rollback_upgrade")
    old_release, old_python, old_sha = obs_linux_service.selection(ctx.home)
    old_root = old_release.resolve(strict=True)
    key = tombstone_key.load_key(ctx.data, create=False)
    before = database_state(ctx.data)
    assert before[0] == store.SCHEMA_VERSION and not before[3]
    prior_pid = obs_status.read_status(ctx.data).get("pid")
    lifecycle.start(ctx, old_python, old_release)
    old_pid = wait_healthy(ctx, prior_pid)
    repo, new_sha = fixture_release(ctx.home.parent, root)
    assert new_sha != old_sha
    alternate = alternate_interpreter(old_python.resolve(strict=True))
    observed: list[int] = []

    def run(
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        result = lifecycle_native_doctor.run(argv, env=env, input=input)
        if "doctor" in argv and not observed:
            report = json.loads(result.stdout)
            assert result.returncode == 0
            assert any(
                entry.get("check") == "managed_service"
                and entry.get("ok") is True
                for entry in report["checks"]
            )
            pid = observe_writer(ctx, new_sha, alternate, old_pid, before)
            observed.append(pid)
            return subprocess.CompletedProcess(argv, 1, result.stdout, b"")
        return result

    upgraded = dataclasses.replace(
        ctx, run=run, ts="native-rollback", fresh=False, upgrade=True
    )
    try:
        with patch.object(sys, "executable", str(alternate)):
            installer.install(upgraded, repo, new_sha)
    except StepFailedError as error:
        assert "verify failed" in str(error) and observed
    else:
        raise AssertionError("synthetic write did not enter recovery")
    retained, python, sha = obs_linux_service.selection(ctx.home)
    assert (
        sha == new_sha
        and python == runtime_python(ctx.lib / new_sha, ctx.platform)
        and python.resolve(strict=True) == alternate
        and retained.resolve().name == new_sha
    )
    assert old_root.is_dir() and retained.resolve(strict=True).is_dir()
    assert database_state(ctx.data) == (*before[:3], True)
    assert tombstone_key.load_key(ctx.data, create=False) == key
    assert platform_io.is_private(ctx.data, directory=True)
    for path in (store.db_path(ctx.data), ctx.data / tombstone_key.KEY_FILE):
        assert platform_io.is_private(path)
    record = json.loads((ctx.lib / installer.INSTALL_RECORD).read_text())
    assert record["failed"] == {"step": "verify", "error": "StepFailedError"}
    assert record["outcome"] == "recovery_required"
    assert record["recovery_required"] is True
    assert record["store"]["phase"] == "current_prepared"
    assert not ((found := lifecycle.job(ctx)) and found["pid"])
    assert obs_linux_service.unit_stopped(obs.run)
    return repo
