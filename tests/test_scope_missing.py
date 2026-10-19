"""Scope resolution for cwds that no longer exist on disk.

Temp repos and temp dirs only; HOME is patched to a temp dir.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from unittest import mock

from muninn import (
    ingest,
    review_quiescence,
    review_quiescence_state,
    scope,
    store,
)
from tests.ingest_support import line
from tests.scope_support import NO_HASH, ScopeCase
from tests.test_classify import codex_meta, reply, user_msg


class MissingCwdTests(ScopeCase):
    """Resolution of cwds that no longer exist."""

    def test_existing_cwd_never_takes_the_prefix_path(self) -> None:
        conn = self.rw()
        repo = self.tmp / "repo"
        self.make_repo(repo)
        repo_id = scope.scope_id(conn, str(repo))
        self.remove_tree(
            repo / ".git"
        )  # the scope stays known, the dir is plain
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
            self.remove_tree(wt)
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

    def test_unprepared_older_clone_precedes_prepared_later_match(
        self,
    ) -> None:
        conn = self.rw()
        old, new = self.tmp / "old", self.tmp / "new"
        hint = self.make_repo(old)
        self.git("clone", "-q", str(old), str(new), cwd=self.tmp)
        oldest = scope.scope_id(conn, str(old))
        scope.scope_id(conn, str(new))
        facts = {scope.CommitLookup(str(new), hint): True}
        with self.assertRaises(scope.ScopePreparationRequiredError) as raised:
            scope.scope_id(
                conn, str(self.tmp / "gone"), hint, commit_facts=facts
            )
        self.assertEqual(raised.exception.lookup, (str(old), hint))
        scope.prepare_commit(raised.exception.lookup, facts)
        self.assertEqual(
            scope.scope_id(
                conn, str(self.tmp / "gone"), hint, commit_facts=facts
            ),
            oldest,
        )

    def test_connection_owned_retry_retains_owner_and_reopens_under_writer(
        self,
    ) -> None:
        self.home.chmod(0o700)
        repo = self.tmp / "repo"
        hint = self.make_repo(repo)
        gone = self.tmp / "gone"
        roots = {"codex-sessions": self.tmp / "sources"}
        roots["codex-sessions"].mkdir()
        records = [
            codex_meta(
                "user", "one", cwd=str(repo), git={"commit_hash": hint}
            ),
            user_msg(1, "first"),
            {"type": "turn_context", "payload": {"cwd": str(gone)}},
            reply(2, "second"),
        ]
        (roots["codex-sessions"] / "rollout-one.jsonl").write_bytes(
            b"".join(map(line, records))
        )
        facts: scope.CommitFacts = {}
        total = ingest.PassStats()
        with review_quiescence.maintenance_owner(
            self.home, wait_s=0, status=review_quiescence.EraseStatus()
        ) as owner:
            deadline = owner.deadline
            self.assertFalse(os.get_inheritable(owner.fd))
            with review_quiescence.maintenance_writer(owner):
                conn = store.connect_rw(
                    store.db_path(self.home), fullfsync=False
                )
                with contextlib.closing(conn):
                    review_quiescence_state.seed_new(self.home, owner.fd)
                    with self.assertRaises(
                        ingest.IngestPreparationRequiredError
                    ) as raised:
                        ingest.ingest(conn, roots, commit_facts=facts)
                    pending = raised.exception
                    total.accumulate(pending.stats)
                    self.assertFalse(conn.in_transaction)
                    for table in ("source", "event", "scope", "scope_path"):
                        self.assertEqual(
                            conn.execute(
                                f"SELECT count(*) FROM {table}"
                            ).fetchone()[0],
                            0,
                        )
                    self.assertEqual(
                        (
                            pending.stats.failed,
                            pending.stats.errors,
                            pending.stats.missing,
                        ),
                        (0, {}, 0),
                    )
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            with self.assertRaises(BlockingIOError):
                review_quiescence_state.acquire(self.home)
            with store.writer_lock(self.home, wait_s=0):
                pass
            scope.prepare_commit(pending.lookup, facts)
            self.assertEqual(owner.deadline, deadline)
            with (
                review_quiescence.maintenance_writer(owner),
                contextlib.closing(
                    store.connect_rw(store.db_path(self.home), fullfsync=False)
                ) as reopened,
            ):
                review_quiescence.verify_current(owner, reopened)
                stats = ingest.ingest(reopened, roots, commit_facts=facts)
                total.accumulate(stats)
                self.assertEqual(
                    reopened.execute("SELECT count(*) FROM event").fetchone()[
                        0
                    ],
                    2,
                )
            self.assertEqual(
                (
                    total.files_seen,
                    total.files_changed,
                    total.events_added,
                    total.failed,
                ),
                (2, 1, 2, 0),
            )

    def test_retry_rechecks_new_cached_cwd_before_prepared_membership(
        self,
    ) -> None:
        conn = self.rw()
        one, two = self.tmp / "one", self.tmp / "two"
        hint = self.make_repo(one)
        self.make_repo(two, "two")
        scope.scope_id(conn, str(one))
        second = scope.scope_id(conn, str(two))
        gone = self.tmp / "gone"
        root = self.tmp / "sources"
        root.mkdir()
        records = [
            codex_meta(
                "user", "one", cwd=str(gone), git={"commit_hash": hint}
            ),
            user_msg(1, "question"),
        ]
        (root / "rollout-one.jsonl").write_bytes(b"".join(map(line, records)))
        probe = scope._has_commit

        def remap(repo: str, commit: str) -> bool:
            answer = probe(repo, commit)
            with store.writer_lock(self.db.parent, wait_s=0):
                conn.execute(
                    "INSERT INTO scope_path(cwd,scope_id,method)"
                    " VALUES(?,?,'git')",
                    (str(gone), second),
                )
            return answer

        with mock.patch.object(scope, "_has_commit", remap):
            stats = ingest.run_pass(
                self.db.parent, {"codex-sessions": root}, fullfsync=False
            )
        self.assertEqual(self.row(conn, gone)[0], str(two))
        self.assertEqual(
            conn.execute("SELECT scope_id FROM event").fetchone()[0], second
        )
        self.assertEqual((stats.files_seen, stats.files_changed), (2, 1))
