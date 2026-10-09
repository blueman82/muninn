"""Actual Linux source wrappers run with isolated provider and state paths."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

from install import installer
from install.context import Ctx
from muninn.obs_linux_service import require_interpreter


def fingerprint(home: Path) -> dict[str, tuple[object, ...]]:
    """Capture synthetic identities, permissions and complete file bytes."""
    result: dict[str, tuple[object, ...]] = {}
    for path in (home, *sorted(home.rglob("*"))):
        info = path.lstat()
        fields: tuple[object, ...] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if stat.S_ISLNK(info.st_mode):
            fields += (str(path.readlink()),)
        elif stat.S_ISREG(info.st_mode):
            fields += (hashlib.sha256(path.read_bytes()).hexdigest(),)
        elif not stat.S_ISDIR(info.st_mode):
            raise OSError("unexpected synthetic state type")
        result[str(path.relative_to(home))] = fields
    return result


def environment(ctx: Ctx) -> dict[str, str]:
    """Keep every provider and runtime path inside the synthetic HOME."""
    return dict(
        os.environ,
        HOME=str(ctx.home),
        USERPROFILE=str(ctx.home),
        CODEX_HOME=str(ctx.codex_home),
        CLAUDE_CONFIG_DIR=str(ctx.settings.parent),
        MUNINN_HOME=str(ctx.data),
        MUNINN_PYTHON=sys.executable,
    )


def run_wrapper(
    ctx: Ctx, root: Path, name: str, *args: str
) -> subprocess.CompletedProcess[bytes]:
    """Execute the dedicated source wrapper with a trusted interpreter."""
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    if name not in {"muninn-install", "muninn-uninstall"}:
        raise ValueError("dedicated source wrapper required")
    require_interpreter(Path(sys.executable))
    result = subprocess.run(
        [root / "bin" / name, *args],
        env=environment(ctx),
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result


def readonly(ctx: Ctx, root: Path, name: str, *args: str) -> bytes:
    """Require checks and uninstall dry runs to leave state unchanged."""
    if (name, args) not in {
        ("muninn-install", ("--check",)),
        ("muninn-install", ("--status",)),
        ("muninn-uninstall", ("--dry-run",)),
    }:
        raise ValueError("readonly dedicated wrapper required")
    before = fingerprint(ctx.home)
    result = run_wrapper(ctx, root, name, *args)
    assert fingerprint(ctx.home) == before, "readonly wrapper changed home"
    return result.stdout


def install(ctx: Ctx, root: Path, sha: str) -> None:
    """Run native source auto-mode checks and installs."""
    if sys.platform != "linux":
        installer.install(ctx, root, sha)
        return
    head = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    assert head == sha, "source wrapper checkout differs from tested SHA"
    readonly(ctx, root, "muninn-install", "--check")
    run_wrapper(ctx, root, "muninn-install")


def status(ctx: Ctx, root: Path, sha: str) -> None:
    """Require source status to describe the actual selected checkout HEAD."""
    result = readonly(ctx, root, "muninn-install", "--status")
    expected = (
        f"installed {sha[:7]}, checkout {sha[:7]}: up to date\n".encode()
    )
    assert result == expected, "source status differs from selected SHA"


def maintenance_checks(ctx: Ctx, root: Path) -> None:
    """Prove status and uninstall dry run after the managed writer quiesces."""
    status(ctx, root, ctx.release.resolve(strict=True).name)
    readonly(ctx, root, "muninn-uninstall", "--dry-run")
