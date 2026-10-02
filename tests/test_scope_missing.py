"""Scope resolution for cwds that no longer exist on disk.

Temp repos and temp dirs only; HOME is patched to a temp dir.
"""

from __future__ import annotations

import os
import shutil
from unittest import mock

from muninn import scope
from tests.scope_support import NO_HASH, ScopeCase


class MissingCwdTests(ScopeCase):
    """Resolution of cwds that no longer exist."""

    def test_existing_cwd_never_takes_the_prefix_path(self) -> None:
        conn = self.rw()
        repo = self.tmp / "repo"
        self.make_repo(repo)
        repo_id = scope.scope_id(conn, str(repo))
        shutil.rmtree(repo / ".git")  # the scope stays known, the dir is plain
        fresh = repo / "fresh"
        fresh.mkdir()
        self.assertNotEqual(scope.scope_id(conn, str(fresh)), repo_id)
        self.assertEqual(self.row(conn, fresh), (str(fresh), "dir", "cwd"))

    def test_prefix_fallback_never_home_or_nongit(self) -> None:
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

    def test_missing_cwd_resolves_by_commit_hint_then_prefix_then_bare(
        self,
    ) -> None:
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

    def test_resolve_key_takes_the_known_repos_for_a_hint(self) -> None:
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

    def test_hint_held_by_several_known_repos_goes_to_the_first_known(
        self,
    ) -> None:
        conn = self.rw()
        one, clone = self.tmp / "one", self.tmp / "clone"
        hash_one = self.make_repo(one, "first")
        self.git("clone", "-q", str(one), str(clone), cwd=self.tmp)
        id_clone = scope.scope_id(conn, str(clone))  # known first
        scope.scope_id(conn, str(one))
        cwd = self.tmp / "away" / "gone"
        got = scope.scope_id(conn, str(cwd), commit_hint=hash_one)
        self.assertEqual(got, id_clone)
