"""Scope identity: one key per repo across worktrees, subdirs and symlinks.

Temp repos and temp dirs only; HOME is patched to a temp dir.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pctx import scope, store

NO_HASH = "0" * 40


class ScopeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))  # /var -> /private/var
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.db = self.tmp / "db" / "pctx.sqlite"

    def rw(self):
        conn = store.connect_rw(self.db, fullfsync=False)
        self.addCleanup(conn.close)
        return conn

    def ro(self):
        conn = store.connect_ro(self.db)
        self.addCleanup(conn.close)
        return conn

    def git(self, *args, cwd):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        cmd = [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
        ]
        cmd += ["-c", "commit.gpgsign=false", *args]
        done = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def make_repo(self, path, message="init"):
        """A repo at path with one commit; returns its HEAD hash."""
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", cwd=path)
        self.git("commit", "-q", "--allow-empty", "-m", message, cwd=path)
        return self.git("rev-parse", "HEAD", cwd=path)

    def row(self, conn, cwd):
        """(scope key, scope kind, method) cached for cwd, or None."""
        found = conn.execute(
            "SELECT s.key, s.kind, sp.method FROM scope_path sp"
            " JOIN scope s ON s.id = sp.scope_id WHERE sp.cwd = ?",
            (str(cwd),),
        ).fetchone()
        return tuple(found) if found else None


class MatrixTests(ScopeCase):
    def test_scope_matrix(self):
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

    @unittest.skipUnless(os.path.islink("/tmp"), "/tmp is not a symlink")
    def test_tmp_and_private_tmp_share_a_key(self):
        a, b = "/tmp/pctx-scope-x", "/private/tmp/pctx-scope-x"
        self.assertEqual(scope.resolve_key(a), scope.resolve_key(b))
        self.assertEqual(scope.resolve_key(a)[0], b)
        conn = self.rw()
        self.assertEqual(scope.scope_id(conn, a), scope.scope_id(conn, b))

    def test_dotgit_pointer_files(self):
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
    def test_global_scope_singleton(self):
        conn = self.rw()
        first = scope.global_scope_id(conn)
        self.assertEqual(scope.global_scope_id(conn), first)
        found = conn.execute("SELECT key, label, kind FROM scope")
        self.assertEqual(
            [tuple(r) for r in found], [("global", "global", "global")]
        )
        self.assertEqual(scope.global_scope_id(self.ro()), first)  # readers

    def test_scope_ids_for_read_never_inserts(self):
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

    def test_unusable_cwd_shares_one_unknown_scope(self):
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


class MissingCwdTests(ScopeCase):
    def test_existing_cwd_never_takes_the_prefix_path(self):
        conn = self.rw()
        repo = self.tmp / "repo"
        self.make_repo(repo)
        repo_id = scope.scope_id(conn, str(repo))
        shutil.rmtree(repo / ".git")  # the scope stays known, the dir is plain
        fresh = repo / "fresh"
        fresh.mkdir()
        self.assertNotEqual(scope.scope_id(conn, str(fresh)), repo_id)
        self.assertEqual(self.row(conn, fresh), (str(fresh), "dir", "cwd"))

    def test_prefix_fallback_never_home_or_nongit(self):
        conn = self.rw()
        with self.subTest("a known git scope at $HOME is never a prefix"):
            self.make_repo(self.home)  # e.g. a dotfiles repo
            home_scope = scope.scope_id(conn, str(self.home))
            self.assertEqual(self.row(conn, self.home)[1], "git")
            gone = self.home / "gone" / "project"
            self.assertNotEqual(scope.scope_id(conn, str(gone)), home_scope)
            self.assertEqual(self.row(conn, gone), (str(gone), "dir", "cwd"))
        with self.subTest("a known non-git ancestor is never a prefix"):
            notes = self.tmp / "work" / "notes"
            notes.mkdir(parents=True)
            notes_scope = scope.scope_id(conn, str(notes))
            self.assertEqual(self.row(conn, notes)[1], "dir")
            gone = notes / "gone" / "x"
            self.assertNotEqual(scope.scope_id(conn, str(gone)), notes_scope)
            self.assertEqual(self.row(conn, gone), (str(gone), "dir", "cwd"))
        with self.subTest("known git scopes are used, longest first"):
            outer = self.tmp / "outer"
            inner = outer / "inner"
            self.make_repo(outer, "outer")
            self.make_repo(inner, "inner")
            outer_id = scope.scope_id(conn, str(outer))
            inner_id = scope.scope_id(conn, str(inner))
            self.assertNotEqual(outer_id, inner_id)
            deep = inner / "gone" / "a"
            self.assertEqual(scope.scope_id(conn, str(deep)), inner_id)
            other = outer / "other" / "gone"
            self.assertEqual(scope.scope_id(conn, str(other)), outer_id)
            self.assertEqual(self.row(conn, other)[2], "prefix")
        with self.subTest("a cached worktree path is a known prefix"):
            wt = self.tmp / "wt"
            self.git("worktree", "add", "-q", "-b", "f", str(wt), cwd=outer)
            wt_id = scope.scope_id(conn, str(wt))
            shutil.rmtree(wt)
            deep = wt / "gone" / "sub"
            self.assertEqual(scope.scope_id(conn, str(deep)), wt_id)
            self.assertEqual(self.row(conn, deep)[2], "prefix")

    def test_missing_cwd_resolves_by_commit_hint_then_prefix_then_bare(self):
        conn = self.rw()
        one, two = self.tmp / "one", self.tmp / "two"
        hash_one = self.make_repo(one, "first")
        hash_two = self.make_repo(two, "second")
        id_one = scope.scope_id(conn, str(one))
        id_two = scope.scope_id(conn, str(two))
        away = self.tmp / "away"
        with self.subTest(
            "1: a commit hint maps to the known repo holding it"
        ):
            cwd = away / "a"
            self.assertEqual(
                scope.scope_id(conn, str(cwd), commit_hint=hash_one), id_one
            )
            self.assertEqual(self.row(conn, cwd), (str(one), "git", "git"))
            cwd = away / "b"
            self.assertEqual(
                scope.scope_id(conn, str(cwd), commit_hint=hash_two), id_two
            )
        with self.subTest("the lookup ignores GIT_* set by a calling hook"):
            with mock.patch.dict(os.environ, {"GIT_DIR": str(self.tmp / "x")}):
                cwd = away / "c"
                got = scope.scope_id(conn, str(cwd), commit_hint=hash_one)
            self.assertEqual(got, id_one)
        with self.subTest("1 beats 2: the hint wins over a path prefix"):
            cwd = two / "gone"
            self.assertEqual(
                scope.scope_id(conn, str(cwd), commit_hint=hash_one), id_one
            )
        with self.subTest("2: no usable hint falls back to the path prefix"):
            hints = (None, "", NO_HASH, "not-a-hash", "--upload-pack=x", "abc")
            hints += ("HEAD", hash_one[:12])  # revisions, not full hashes
            hints += (self.git("rev-parse", "HEAD^{tree}", cwd=one),)
            for n, hint in enumerate(hints):
                cwd = two / f"gone{n}"
                got = scope.scope_id(conn, str(cwd), commit_hint=hint)
                self.assertEqual(got, id_two, hint)
                self.assertEqual(self.row(conn, cwd)[2], "prefix")
        with self.subTest("only git-kind scopes are probed for the commit"):
            late = self.tmp / "late"
            late.mkdir()
            id_late = scope.scope_id(conn, str(late))  # plain when first seen
            hash_late = self.make_repo(late, "third")  # a repo only later
            cwd = away / "d"
            got = scope.scope_id(conn, str(cwd), commit_hint=hash_late)
            self.assertNotEqual(got, id_late)
            self.assertEqual(self.row(conn, cwd), (str(cwd), "dir", "cwd"))
        with self.subTest("3: otherwise the bare cwd"):
            cwd = self.tmp / "nowhere" / "gone"
            got = scope.scope_id(conn, str(cwd), commit_hint=NO_HASH)
            self.assertNotIn(got, (id_one, id_two))
            self.assertEqual(self.row(conn, cwd), (str(cwd), "dir", "cwd"))
        with self.subTest("the hint is ignored for a cwd that still exists"):
            plain = self.tmp / "plain"
            plain.mkdir()
            got = scope.scope_id(conn, str(plain), commit_hint=hash_one)
            self.assertNotEqual(got, id_one)
            self.assertEqual(self.row(conn, plain), (str(plain), "dir", "cwd"))

    def test_resolve_key_takes_the_known_repos_for_a_hint(self):
        one, two = self.tmp / "one", self.tmp / "two"
        hash_one = self.make_repo(one, "first")
        self.make_repo(two, "second")
        cwd = str(self.tmp / "away" / "gone")
        repos = [str(two), str(one)]
        want = (str(one), "git", "git")
        self.assertEqual(scope.resolve_key(cwd, hash_one, repos=repos), want)
        bare = (cwd, "dir", "cwd")
        self.assertEqual(scope.resolve_key(cwd, hash_one), bare)
        self.assertEqual(scope.resolve_key(cwd, NO_HASH, repos=repos), bare)
        self.assertEqual(scope.resolve_key(cwd, None, repos=repos), bare)
        gone_repo = str(self.tmp / "vanished")  # unreadable repo: no crash
        self.assertEqual(
            scope.resolve_key(cwd, hash_one, repos=[gone_repo]), bare
        )

    def test_hint_held_by_several_known_repos_goes_to_the_first_known(self):
        conn = self.rw()
        one, clone = self.tmp / "one", self.tmp / "clone"
        hash_one = self.make_repo(one, "first")
        self.git("clone", "-q", str(one), str(clone), cwd=self.tmp)
        id_clone = scope.scope_id(conn, str(clone))  # known first
        scope.scope_id(conn, str(one))
        cwd = self.tmp / "away" / "gone"
        got = scope.scope_id(conn, str(cwd), commit_hint=hash_one)
        self.assertEqual(got, id_clone)


if __name__ == "__main__":
    unittest.main()
