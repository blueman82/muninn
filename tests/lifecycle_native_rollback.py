"""Actual managed migration rollback across a synthetic release and path."""

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
from muninn import (
    obs,
    obs_linux_service,
    obs_status,
    platform_io,
    tombstone_key,
)
from tests import lifecycle_native_doctor
from tests.native_diagnostics import write

_ANCHOR = "    if version in (1, 2):\n"
_MIGRATION = (
    "    if version == 3:\n"
    "        conn.executescript(\n"
    '            "BEGIN IMMEDIATE;"\n'
    '            "CREATE TABLE native_upgrade_marker(value INTEGER);"\n'
    '            "INSERT INTO native_upgrade_marker VALUES(1);"\n'
    '            "PRAGMA user_version=4;COMMIT;"\n'
    "        )\n"
    "        return\n"
)


def migration_source(source: str) -> str:
    """Insert one real fixture migration only at the known current boundary."""
    if source.count(_ANCHOR) != 1:
        raise ValueError("fixture_source_changed")
    return source.replace(_ANCHOR, _MIGRATION + _ANCHOR)


def fixture_release(parent: Path, root: Path) -> tuple[Path, str]:
    """Create a clean private release with a transactional schema change."""
    repo = parent / "rollback-repo"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(root), str(repo)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    schema = repo / "muninn/store_schema.py"
    source = schema.read_text()
    assert source.count("SCHEMA_VERSION = 3") == 1
    schema.write_text(
        source.replace("SCHEMA_VERSION = 3", "SCHEMA_VERSION = 4")
    )
    store = repo / "muninn/store.py"
    store.write_text(migration_source(store.read_text()))
    for argv in (
        ["config", "user.name", "Muninn Native Fixture"],
        ["config", "user.email", "native-fixture@example.invalid"],
        ["add", "muninn/store.py", "muninn/store_schema.py"],
        ["commit", "-m", "Synthetic transactional native migration"],
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
        snapshot_io.validated(database),
        closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        ) as conn,
    ):
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
    """Require migrated rows and the distinct executable/root identity."""
    state = database_state(ctx.data)
    assert state == (4, before[1], before[2], True)
    selected, executable, sha = obs_linux_service.selection(ctx.home)
    assert sha == new_sha and executable == alternate
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


def rollback(ctx: Ctx, root: Path) -> None:
    """Require a real new writer/migration and an old writer after rollback."""
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    write("lifecycle", "rollback_upgrade")
    old_release, old_python, old_sha = obs_linux_service.selection(ctx.home)
    key = tombstone_key.load_key(ctx.data, create=False)
    before = database_state(ctx.data)
    assert before[0] == 3 and not before[3]
    prior_pid = obs_status.read_status(ctx.data).get("pid")
    lifecycle.start(ctx, old_python, old_release)
    old_pid = wait_healthy(ctx, prior_pid)
    repo, new_sha = fixture_release(ctx.home.parent, root)
    assert new_sha != old_sha
    alternate = alternate_interpreter(old_python)
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
        raise AssertionError("synthetic migration did not enter rollback")
    restored, python, sha = obs_linux_service.selection(ctx.home)
    assert (
        sha == old_sha
        and python == old_python
        and restored.resolve() == old_release.resolve()
    )
    assert database_state(ctx.data) == before
    assert tombstone_key.load_key(ctx.data, create=False) == key
    pid = wait_healthy(ctx, observed[0])
    assert obs_linux_service.process_identity(pid)[1] == old_python
    lifecycle.stop(ctx)
    assert not ((found := lifecycle.job(ctx)) and found["pid"])
