"""Repo scope identity: worktrees and subdirs of one repo share one key."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import cast

GLOBAL_KEY = "global"
# A full-length object id only: an abbreviated hint could match the wrong
# repo, and the value reaches `git cat-file`, so it must be plain hex.
_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _gitfile_repo(root: str, dotgit: Path) -> tuple[str, str] | None:
    """Return (key, method) for a `.git` file, or None if it is not one.

    The file belongs to a linked worktree or to a repo with a separate git
    dir.
    """
    try:
        with dotgit.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline(4096)  # a gitdir line is short
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(root, first[7:].strip()).resolve()
    worktrees = gitdir.parent
    if worktrees.name != "worktrees":
        return root, "git"  # submodule or separate git dir: its own root
    # <main>/.git/worktrees/<name> -> <main>
    # ponytail: a bare repo's worktrees resolve to the bare repo's parent
    # dir; read `commondir` instead if that layout ever matters.
    return str(worktrees.parent.parent), "worktree"


def _enclosing_repo(path: Path) -> tuple[str, str] | None:
    """Return (key, method) of the nearest repo at or above an existing dir."""
    while True:
        dotgit = path / ".git"
        if dotgit.is_dir():
            return str(path), "git"
        if dotgit.is_file():
            found = _gitfile_repo(str(path), dotgit)
            if found:
                return found
        parent = path.parent
        if parent == path:
            return None
        path = parent


def _has_commit(repo: str, commit: str) -> bool:
    """Whether the repo's object store holds this commit."""
    # An inherited GIT_DIR or GIT_WORK_TREE would override `-C repo` and
    # answer for some other repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", f"{commit}^{{commit}}"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def resolve_key(
    cwd: str,
    commit_hint: str | None = None,
    *,
    repos: Iterable[str] = (),
) -> tuple[str, str, str]:
    """Resolve a cwd to a scope key from the filesystem alone.

    For a cwd that no longer exists, a full-length commit_hint (Codex's
    git.commit_hash) is looked up in ``repos``, the known git scope keys; the
    first repo holding the commit wins. An existing cwd ignores the hint.
    A cwd that is not an absolute path shares the single key 'unknown'.

    Args:
        cwd: Working directory recorded in a transcript.
        commit_hint: Commit id recorded beside the cwd, if any.
        repos: Known git scope keys, searched for ``commit_hint``.

    Returns:
        (key, kind ``git|dir``, method ``git|worktree|cwd``).
    """
    if not Path(cwd).is_absolute() or "\0" in cwd:
        return "unknown", "dir", "cwd"  # never resolve against our own cwd
    path = Path(cwd).resolve()
    if path.is_dir():
        found = _enclosing_repo(path)
        if found:
            return found[0], "git", found[1]
    elif commit_hint and _COMMIT.fullmatch(commit_hint):
        for repo in repos:
            if _has_commit(repo, commit_hint):
                return repo, "git", "git"
    return str(path), "dir", "cwd"


def _home() -> str:
    """Return the resolved home dir, which a prefix match must never hit."""
    return str(Path.home().resolve())


def _cached(conn: sqlite3.Connection, cwd: str) -> int | None:
    """Return the scope id already recorded for this exact cwd string."""
    row = conn.execute(
        "SELECT scope_id FROM scope_path WHERE cwd = ?", (cwd,)
    ).fetchone()
    return row[0] if row else None


def _prefix_scope(conn: sqlite3.Connection, path: str) -> int | None:
    """Return the longest known git scope on the way up from a missing path.

    Only kind 'git' scopes qualify (a worktree folds into its repo's), and
    never $HOME: a prefix hit must not pull a project into a broad parent.
    Known paths are scope keys and cached cwds (e.g. a deleted worktree).
    """
    params = {"home": _home()}
    current = Path(path)
    while True:  # the path itself first: it may be a cached deleted dir
        params["path"] = str(current)
        row = conn.execute(
            "SELECT id FROM scope WHERE kind = 'git' AND key = :path"
            " AND key != :home"
            " UNION SELECT sp.scope_id FROM scope_path sp"
            " JOIN scope s ON s.id = sp.scope_id"
            " WHERE sp.cwd = :path AND s.kind = 'git' AND s.key != :home",
            params,
        ).fetchone()
        if row:
            return row[0]
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _git_roots(conn: sqlite3.Connection) -> list[str]:
    """Return every known git scope key, oldest scope first."""
    rows = conn.execute("SELECT key FROM scope WHERE kind = 'git' ORDER BY id")
    return [row[0] for row in rows]


def _resolve(
    conn: sqlite3.Connection, cwd: str, commit_hint: str | None = None
) -> tuple[int | None, str, str, str]:
    """Resolve a cwd without writing anything.

    A gone cwd resolves by commit hint, then known-git-scope prefix, then
    as its bare self.

    Returns:
        (scope id if already known, key, kind, method).
    """
    repos = _git_roots(conn) if commit_hint else ()
    key, kind, method = resolve_key(cwd, commit_hint, repos=repos)
    if method == "cwd" and Path(key).is_absolute() and not Path(key).is_dir():
        sid = _prefix_scope(conn, key)  # the cwd is gone
        if sid is not None:
            return sid, key, kind, "prefix"
    row = conn.execute("SELECT id FROM scope WHERE key = ?", (key,)).fetchone()
    return (row[0] if row else None), key, kind, method


def _new_scope(conn: sqlite3.Connection, key: str, kind: str) -> int:
    """Insert a scope row labelled by its last path component."""
    label = Path(key).name or key
    new_id = conn.execute(
        "INSERT INTO scope(key, label, kind) VALUES (?, ?, ?)",
        (key, label, kind),
    ).lastrowid
    # An INSERT that did not raise always sets lastrowid; typeshed types it
    # as Optional because it is None after other statement kinds.
    return cast(int, new_id)


def scope_id(
    conn: sqlite3.Connection, cwd: str, commit_hint: str | None = None
) -> int:
    """Return the scope id for a cwd, creating it on first sight.

    The cwd to scope mapping is cached in scope_path so later lines with the
    same cwd cost one lookup and no filesystem access.  The caller must hold
    a write transaction.

    Args:
        conn: Read-write store connection.
        cwd: Exact working directory string from the transcript.
        commit_hint: Commit id used to place a deleted cwd in its repo.

    Returns:
        The scope row id.
    """
    sid = _cached(conn, cwd)
    if sid is not None:
        return sid
    sid, key, kind, method = _resolve(conn, cwd, commit_hint)
    if sid is None:
        sid = _new_scope(conn, key, kind)
    conn.execute(
        "INSERT OR IGNORE INTO scope_path(cwd, scope_id, method)"
        " VALUES (?, ?, ?)",
        (cwd, sid, method),
    )
    return sid


def _global(conn: sqlite3.Connection) -> int | None:
    """Return the id of the 'global' scope, or None if it does not exist."""
    row = conn.execute(
        "SELECT id FROM scope WHERE key = ?", (GLOBAL_KEY,)
    ).fetchone()
    return row[0] if row else None


def global_scope_id(conn: sqlite3.Connection) -> int:
    """Return the singleton 'global' scope id, creating it if needed."""
    found = _global(conn)
    return (
        found if found is not None else _new_scope(conn, GLOBAL_KEY, "global")
    )


def scope_ids_for_read(conn: sqlite3.Connection, cwd: str) -> list[int]:
    """Return the [repo scope, global scope] ids that exist for a cwd.

    Read-only: nothing is inserted, so a scope never seen at ingest is
    simply absent from the result.
    """
    sid = _cached(conn, cwd)
    if sid is None:
        sid = _resolve(conn, cwd)[0]
    ids = [] if sid is None else [sid]
    wide = _global(conn)
    return ids if wide is None else [*ids, wide]
