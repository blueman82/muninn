"""Scope identity: one key per repo across worktrees, subdirs and symlinks.

Temp repos and temp dirs only; HOME is patched to a temp dir.
"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
from pathlib import Path

from muninn import scope
from tests.scope_support import ScopeCase


class MatrixTests(ScopeCase):
    """Key resolution across repos, worktrees, links and the cache."""

    def test_scope_matrix(self) -> None:
        conn = self.rw()
        repo = self.tmp / "repo"
        self.make_repo(repo)
        sub = repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=repo)
        (wt / "inner").mkdir()
        plain = self.tmp / "plain"
        plain.mkdir()
        key = str(repo)
        with self.subTest("repo root"):
            self.assertEqual(scope.resolve_key(key), (key, "git", "git"))
        with self.subTest("subdirectory folds into the repo key"):
            self.assertEqual(scope.resolve_key(str(sub)), (key, "git", "git"))
        with self.subTest("worktree and its subdir fold into the main repo"):
            want = (key, "git", "worktree")
            self.assertEqual(scope.resolve_key(str(wt)), want)
            self.assertEqual(scope.resolve_key(str(wt / "inner")), want)
        with self.subTest("non-git dir keys on itself with kind dir"):
            want = (str(plain), "dir", "cwd")
            self.assertEqual(scope.resolve_key(str(plain)), want)
        with self.subTest("home dir is its own scope"):
            want = (str(self.home), "dir", "cwd")
            self.assertEqual(scope.resolve_key(str(self.home)), want)
        with self.subTest("symlink spelling has the same key"):
            link = self.tmp / "link"
            link.symlink_to(repo)
            want = (key, "git", "git")
            self.assertEqual(scope.resolve_key(str(link / "pkg")), want)
        with self.subTest("ids: repo, subdir and worktree share one scope"):
            ids = {scope.scope_id(conn, str(p)) for p in (repo, sub, wt)}
            self.assertEqual(len(ids), 1)
            found = conn.execute("SELECT key, label, kind FROM scope")
            self.assertEqual([tuple(r) for r in found], [(key, "repo", "git")])
            self.assertEqual(self.row(conn, wt)[2], "worktree")
        self.check_cache(conn, repo, wt, plain)

    def check_cache(
        self,
        conn: sqlite3.Connection,
        repo: Path,
        wt: Path,
        plain: Path,
    ) -> None:
        """Check what the scope cache pins once a path was resolved.

        Args:
            conn: Open read-write store connection.
            repo: Main repo that already has a scope.
            wt: Linked worktree of ``repo`` that already has a scope.
            plain: Non-git directory; its scope is created by this check.
        """
        key = str(repo)
        with self.subTest("a deleted worktree resolves from the cache"):
            before = scope.scope_id(conn, str(wt))
            shutil.rmtree(wt)
            uncached = scope.resolve_key(str(wt))  # control: no cache, no repo
            self.assertEqual(uncached, (str(wt), "dir", "cwd"))
            self.assertEqual(scope.scope_id(conn, str(wt)), before)
        with self.subTest("the first resolution stays pinned"):
            late = self.tmp / "late"
            late.mkdir()
            first = scope.scope_id(conn, str(late))
            (late / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
            fresh = scope.resolve_key(str(late))  # control: now a worktree
            self.assertEqual(fresh, (key, "git", "worktree"))
            self.assertEqual(scope.scope_id(conn, str(late)), first)
            self.assertNotEqual(first, scope.scope_id(conn, key))
        with self.subTest("deleted never-seen cwd under a known repo: prefix"):
            gone = repo / "gone" / "dir"
            self.assertEqual(
                scope.scope_id(conn, str(gone)), scope.scope_id(conn, key)
            )
            self.assertEqual(self.row(conn, gone), (key, "git", "prefix"))
        with self.subTest("non-git dir and home get their own scopes"):
            ids = {scope.scope_id(conn, str(p)) for p in (plain, self.home)}
            self.assertEqual(len(ids), 2)
            self.assertNotIn(scope.scope_id(conn, key), ids)
            self.assertEqual(
                self.row(conn, self.home), (str(self.home), "dir", "cwd")
            )

    @unittest.skipUnless(Path("/tmp").is_symlink(), "/tmp is not a symlink")
    def test_tmp_and_private_tmp_share_a_key(self) -> None:
        a, b = "/tmp/muninn-scope-x", "/private/tmp/muninn-scope-x"
        self.assertEqual(scope.resolve_key(a), scope.resolve_key(b))
        self.assertEqual(scope.resolve_key(a)[0], b)
        conn = self.rw()
        self.assertEqual(scope.scope_id(conn, a), scope.scope_id(conn, b))

    def test_dotgit_pointer_files(self) -> None:
        repo, wt = self.tmp / "repo", self.tmp / "wt"
        self.make_repo(repo)
        self.git("worktree", "add", "-q", "-b", "f", str(wt), cwd=repo)
        with self.subTest("relative gitdir pointer"):
            (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n")
            want = (str(repo), "git", "worktree")
            self.assertEqual(scope.resolve_key(str(wt)), want)
        with self.subTest("submodule-style pointer is its own repo root"):
            sub = repo / "sub"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
            want = (str(sub), "git", "git")
            self.assertEqual(scope.resolve_key(str(sub)), want)
        with self.subTest("a .git file that is not a pointer is ignored"):
            junk = repo / "junk"
            junk.mkdir()
            (junk / ".git").write_text("hello\n")
            want = (str(repo), "git", "git")
            self.assertEqual(scope.resolve_key(str(junk)), want)


class LookupTests(ScopeCase):
    """Scope id lookup for writers, readers and unusable cwds."""

    def test_global_scope_singleton(self) -> None:
        conn = self.rw()
        first = scope.global_scope_id(conn)
        self.assertEqual(scope.global_scope_id(conn), first)
        found = conn.execute("SELECT key, label, kind FROM scope")
        self.assertEqual(
            [tuple(r) for r in found], [("global", "global", "global")]
        )
        self.assertEqual(scope.global_scope_id(self.ro()), first)  # readers

    def test_scope_ids_for_read_never_inserts(self) -> None:
        rw = self.rw()
        repo = self.tmp / "repo"
        self.make_repo(repo)
        (repo / "pkg").mkdir()
        plain = self.tmp / "plain"
        plain.mkdir()
        known = scope.scope_id(rw, str(repo))
        self.assertEqual(scope.scope_ids_for_read(rw, str(repo)), [known])
        wide = scope.global_scope_id(rw)  # now readers also see global
        late = self.tmp / "late"
        late.mkdir()
        pinned = scope.scope_id(rw, str(late))  # plain when first seen
        (late / ".git").write_text(f"gitdir: {repo}/.git/worktrees/x\n")
        counts = (
            "SELECT (SELECT count(*) FROM scope),"
            " (SELECT count(*) FROM scope_path)"
        )
        before = tuple(rw.execute(counts).fetchone())
        cases = {
            "cached cwd": (repo, [known, wide]),
            "uncached subdirectory": (repo / "pkg", [known, wide]),
            "missing under a known repo": (repo / "gone" / "x", [known, wide]),
            "cache wins over a fresh resolution": (late, [pinned, wide]),
            "unknown existing dir": (plain, [wide]),
            "unusable cwd": ("relative", [wide]),
        }
        for label, (cwd, want) in cases.items():
            for kind, conn in (("rw", rw), ("ro", self.ro())):
                with self.subTest(label, conn=kind):
                    self.assertEqual(
                        scope.scope_ids_for_read(conn, str(cwd)), want
                    )
        self.assertEqual(tuple(rw.execute(counts).fetchone()), before)

    def test_unusable_cwd_shares_one_unknown_scope(self) -> None:
        cases = ("", "relative/dir", "a\0b")
        for cwd in cases:
            with self.subTest(repr(cwd)):
                want = ("unknown", "dir", "cwd")
                self.assertEqual(scope.resolve_key(cwd), want)
        conn = self.rw()
        ids = {scope.scope_id(conn, cwd) for cwd in cases}
        self.assertEqual(len(ids), 1)
        # a cwd spelled "global" must not land in the global scope
        self.assertNotEqual(
            scope.scope_id(conn, "global"), scope.global_scope_id(conn)
        )


if __name__ == "__main__":
    unittest.main()
