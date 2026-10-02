"""Preflight: check everything and dry-apply every config edit.

Nothing on disk is changed here. A failure at this stage costs nothing to
undo, which is why every refusable condition is checked before step one.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from install.constants import CLAUDE_EVENTS, GIT_ENV, PINNED
from install.context import Ctx, StepFailedError, must
from install.record import Record
from install.transforms import (
    drop_trust,
    edit_settings,
    enable,
    repoint,
    write_trust,
)
from install.trust import codex_hooks

CODEX_HOOKS_FILE = "integrations/codex/hooks/hooks.json"


def _git(ctx: Ctx, repo: Path, *args: str) -> bytes:
    """Run a read-only git command in ``repo`` and return its stdout."""
    return must(ctx, ["git", "-C", repo, *args], env=GIT_ENV).stdout


def _check_repo(
    ctx: Ctx, repo: Path, sha: str
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Check the repo is clean at ``sha`` and read the pinned files.

    Files come from git at ``sha``, not the working tree, so what we
    validate is exactly what ``pin`` later archives.

    Args:
        ctx: The run context.
        repo: The source repository.
        sha: The full commit id to install.

    Returns:
        The pinned files' bytes keyed by repo-relative path, with the
        ``@HOME@`` placeholder already substituted, and the Claude hook
        groups per event parsed from the first of them.

    Raises:
        StepFailedError: If HEAD is not ``sha``, the worktree is dirty,
            ``bin/pctx`` is not executable, or the Claude hook fragment has
            the wrong events.
    """
    if _git(ctx, repo, "rev-parse", "HEAD").decode().strip() != sha:
        raise StepFailedError("repo HEAD is not --sha")
    if _git(ctx, repo, "status", "--porcelain", "--untracked-files=all"):
        raise StepFailedError("repo worktree is not clean")
    if not _git(ctx, repo, "ls-tree", sha, "bin/pctx").startswith(b"100755 "):
        raise StepFailedError("bin/pctx is not an executable file at --sha")
    home = str(ctx.home).encode()
    files = {
        r: _git(ctx, repo, "show", f"{sha}:{r}").replace(b"@HOME@", home)
        for r in PINNED
    }
    fragment = json.loads(files[PINNED[0]])["hooks"]
    if set(fragment) != set(CLAUDE_EVENTS):
        raise StepFailedError(
            "Claude hook fragment must be SessionStart+UserPromptSubmit"
        )
    return files, fragment


def _exists(path: Path) -> bool:
    """Say whether anything, even a dangling symlink, is at ``path``."""
    return path.exists() or path.is_symlink()


def _check_upgradable(ctx: Ctx) -> None:
    """Refuse an upgrade when there is no existing install to upgrade.

    Args:
        ctx: The run context.

    Raises:
        StepFailedError: If the data dir, plist or current release is gone.
    """
    if not (ctx.data.is_dir() and ctx.plist.exists()):
        raise StepFailedError("nothing to upgrade: no data dir or plist")
    if not (ctx.lib / "current").is_symlink():
        raise StepFailedError("nothing to upgrade: no current release")


def _check_installable(ctx: Ctx) -> None:
    """Refuse a fresh install over an existing one.

    Args:
        ctx: The run context.

    Raises:
        StepFailedError: If Python is too old or pctx data or a plist exist.
    """
    # Compare major and minor explicitly: this guard is for a stray older
    # interpreter even though the repo tooling targets 3.13.
    if (sys.version_info.major, sys.version_info.minor) < (3, 13):
        raise StepFailedError("the installer needs Python 3.13+")
    for path in (ctx.data, ctx.plist):
        if _exists(path):
            raise StepFailedError(f"{path} exists: already installed")


def _check_machine(ctx: Ctx) -> None:
    """Check the machine is in the state this mode expects.

    Args:
        ctx: The run context.

    Raises:
        StepFailedError: If an upgrade has nothing to upgrade, a fresh
            install finds an existing one, a rollback directory already
            exists, or the ``pctx`` link is not a symlink.
    """
    if ctx.upgrade:
        _check_upgradable(ctx)
    if ctx.fresh:
        _check_installable(ctx)
    for path in (ctx.rdir, ctx.failed):
        if _exists(path):
            raise StepFailedError(f"{path} already exists")
    if ctx.pctx.exists() and not ctx.pctx.is_symlink():
        raise StepFailedError(f"{ctx.pctx} is not a symlink")


def _dry_apply(
    ctx: Ctx, files: dict[str, bytes], fragment: dict[str, Any]
) -> None:
    """Run every config transform on the real files and discard the result.

    Any refusal (a layout we do not recognise, a stray marker) surfaces now,
    before the first change.

    Args:
        ctx: The run context.
        files: The pinned files from ``_check_repo``.
        fragment: The Claude hook groups per event.
    """
    if ctx.settings.exists():
        edit_settings(ctx.settings.read_bytes(), fragment)
    if ctx.config.exists():
        text = ctx.config.read_text()
        hooks = codex_hooks(files[CODEX_HOOKS_FILE])
        text = drop_trust(text)
        text = enable(repoint(text, f"{ctx.lib}/current/integrations/codex"))
        write_trust(text, hooks)


def preflight(ctx: Ctx, repo: Path, sha: str) -> Record:
    """Check everything and dry-apply every config edit; change nothing.

    Args:
        ctx: The run context.
        repo: The source repository.
        sha: The full commit id to install.

    Returns:
        The initial run record.

    Raises:
        StepFailedError: If any precondition fails.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StepFailedError("--sha must be a full 40-hex commit id")
    files, fragment = _check_repo(ctx, repo, sha)
    _check_machine(ctx)
    # An upgrade re-pins only; provider config was set up by the fresh run.
    touch = not ctx.upgrade
    if touch:
        _dry_apply(ctx, files, fragment)
    return {
        "fresh": ctx.fresh,
        "upgrade": ctx.upgrade,
        "has_claude": touch and ctx.settings.exists(),
        "has_codex": touch and ctx.config.exists(),
        "schema": 1,
        "ts": ctx.ts,
        "home": str(ctx.home),
        "repo": str(repo),
        "sha": sha,
        "python": {"path": sys.executable, "version": sys.version.split()[0]},
        "rdir": str(ctx.rdir),
        "steps": [],
    }
