"""Repo scope identity (design 3.6): worktrees and subdirs share one key."""

import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable

GLOBAL_KEY = "global"
_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _gitfile_repo(root: str, dotgit: str) -> tuple[str, str] | None:
    """(key, method) for a `.git` file: a linked worktree or its own repo."""
    try:
        with open(dotgit, encoding="utf-8", errors="replace") as handle:
            first = handle.readline(4096)
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = os.path.realpath(os.path.join(root, first[7:].strip()))
    worktrees = os.path.dirname(gitdir)
    if os.path.basename(worktrees) != "worktrees":
        return root, "git"  # submodule or separate git dir: its own root
    # <main>/.git/worktrees/<name> -> <main>
    # ponytail: a bare repo's worktrees resolve to the bare repo's parent
    # dir; read `commondir` instead if that layout ever matters.
    return os.path.dirname(os.path.dirname(worktrees)), "worktree"


def _enclosing_repo(path: str) -> tuple[str, str] | None:
    """(key, method) of the nearest repo at or above an existing dir."""
    while True:
        dotgit = os.path.join(path, ".git")
        if os.path.isdir(dotgit):
            return path, "git"
        if os.path.isfile(dotgit):
            found = _gitfile_repo(path, dotgit)
            if found:
                return found
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _has_commit(repo: str, commit: str) -> bool:
    """Whether the repo's object store holds this commit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", f"{commit}^{{commit}}"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
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
    """(key, kind git|dir, method git|worktree|cwd) from the filesystem.

    For a cwd that no longer exists, a full-length commit_hint (Codex's
    git.commit_hash) is looked up in `repos`, the known git scope keys; the
    first repo holding the commit wins. An existing cwd ignores the hint.
    A cwd that is not an absolute path shares the single key 'unknown'.
    """
    if not os.path.isabs(cwd) or "\0" in cwd:
        return "unknown", "dir", "cwd"  # never resolve against our own cwd
    path = os.path.realpath(cwd)
    if os.path.isdir(path):
        found = _enclosing_repo(path)
        if found:
            return found[0], "git", found[1]
    elif commit_hint and _COMMIT.fullmatch(commit_hint):
        for repo in repos:
            if _has_commit(repo, commit_hint):
                return repo, "git", "git"
    return path, "dir", "cwd"


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def _cached(conn: sqlite3.Connection, cwd: str) -> int | None:
    row = conn.execute(
        "SELECT scope_id FROM scope_path WHERE cwd = ?", (cwd,)
    ).fetchone()
    return row[0] if row else None


def _prefix_scope(conn: sqlite3.Connection, path: str) -> int | None:
    """Longest known git scope on the way up from a missing path.

    Only kind 'git' scopes qualify (a worktree folds into its repo's), and
    never $HOME: a prefix hit must not pull a project into a broad parent.
    Known paths are scope keys and cached cwds (e.g. a deleted worktree).
    """
    params = {"home": _home()}
    while True:  # the path itself first: it may be a cached deleted dir
        params["path"] = path
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
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _git_roots(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT key FROM scope WHERE kind = 'git' ORDER BY id")
    return [row[0] for row in rows]


def _resolve(
    conn: sqlite3.Connection, cwd: str, commit_hint: str | None = None
) -> tuple[int | None, str, str, str]:
    """(scope id if already known, key, kind, method); never writes.

    A gone cwd resolves by commit hint, then known-git-scope prefix, then
    as its bare self.
    """
    repos = _git_roots(conn) if commit_hint else ()
    key, kind, method = resolve_key(cwd, commit_hint, repos=repos)
    if method == "cwd" and os.path.isabs(key) and not os.path.isdir(key):
        sid = _prefix_scope(conn, key)  # the cwd is gone
        if sid is not None:
            return sid, key, kind, "prefix"
    row = conn.execute("SELECT id FROM scope WHERE key = ?", (key,)).fetchone()
    return (row[0] if row else None), key, kind, method


def _new_scope(conn: sqlite3.Connection, key: str, kind: str) -> int:
    label = os.path.basename(key) or key
    return conn.execute(
        "INSERT INTO scope(key, label, kind) VALUES (?, ?, ?)",
        (key, label, kind),
    ).lastrowid


def scope_id(
    conn: sqlite3.Connection, cwd: str, commit_hint: str | None = None
) -> int:
    """Scope id for a cwd, created on first sight and cached in scope_path."""
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
    row = conn.execute(
        "SELECT id FROM scope WHERE key = ?", (GLOBAL_KEY,)
    ).fetchone()
    return row[0] if row else None


def global_scope_id(conn: sqlite3.Connection) -> int:
    """The singleton 'global' scope; readers get it once it exists."""
    found = _global(conn)
    return (
        found if found is not None else _new_scope(conn, GLOBAL_KEY, "global")
    )


def scope_ids_for_read(conn: sqlite3.Connection, cwd: str) -> list[int]:
    """[repo scope, global scope] that already exist for a cwd; no inserts."""
    sid = _cached(conn, cwd)
    if sid is None:
        sid = _resolve(conn, cwd)[0]
    ids = [] if sid is None else [sid]
    wide = _global(conn)
    return ids if wide is None else ids + [wide]
