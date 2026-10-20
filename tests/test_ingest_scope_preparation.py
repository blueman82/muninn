"""Git preparation preserves admitted source work across writer passes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import (
    cli_serve,
    hook_notes,
    ingest,
    ingest_parse,
    ingest_plan,
    obs,
    scope,
    store,
)
from muninn.tombstone_key import TombstoneKeyError
from tests.ingest_support import IngestCase, line
from tests.scope_support import ScopeCase
from tests.test_classify import codex_meta, reply, user_msg


class PreparationTests(IngestCase, ScopeCase):
    """Exercise real Git, writer locks and transactions in temp homes."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "repo"
        self.commit = self.make_repo(self.repo)
        self.sid = scope.scope_id(self.conn, str(self.repo))
        self.gone = self.tmp / "gone"

    def source_file(
        self, name: str, cwd: Path, commit: str | None = None
    ) -> Path:
        """Write two admitted events carrying an explicit cwd and hint."""
        return self.write(
            f"rollout-{name}.jsonl",
            [
                codex_meta(
                    "user", name, cwd=str(cwd), git={"commit_hash": commit}
                ),
                user_msg(1, name),
                reply(2, name + " answer"),
            ],
        )

    def test_run_pass_probes_with_physical_writer_free(self) -> None:
        self.source_file("a", self.gone, self.commit)
        run = scope.subprocess.run
        observed: list[tuple[bool, object, object]] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            free = True
            try:
                with store.writer_lock(self.home, wait_s=0):
                    pass
            except store.BusyError:
                free = False
            observed.append(
                (free, kwargs.get("close_fds"), kwargs.get("timeout"))
            )
            return run(*args, **kwargs)

        with mock.patch.object(scope.subprocess, "run", spy):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(observed, [(True, True, 10)])
        self.assertEqual(
            (stats.files_seen, stats.files_changed, stats.failed), (2, 1, 0)
        )
        self.assertEqual(self.row(self.conn, self.gone)[0], str(self.repo))

    def test_nested_run_pass_rejects_without_probe(self) -> None:
        self.source_file("a", self.gone, self.commit)
        with (
            store.writer_lock(self.home, wait_s=0),
            mock.patch.object(scope.subprocess, "run") as probe,
            self.assertRaises(store.BusyError),
        ):
            ingest.run_pass(self.home, self.roots, wait_s=0)
        probe.assert_not_called()

    def test_preparation_rolls_back_source_and_stops_before_later_and_missing(
        self,
    ) -> None:
        removed = self.source_file("old", self.repo)
        ingest.run_pass(self.home, self.roots, fullfsync=False)
        removed.unlink()
        self.source_file("a", self.repo)
        path = self.source_file("b", self.gone, self.commit)
        (self.tmp / "transient").mkdir()
        initial = codex_meta(
            "user",
            "b",
            cwd=str(self.tmp / "transient"),
            git={"commit_hash": self.commit},
        )
        context = {"type": "turn_context", "payload": {"cwd": str(self.gone)}}
        path.write_bytes(
            b"".join(
                map(
                    line,
                    [
                        initial,
                        user_msg(1, "first"),
                        context,
                        reply(2, "second"),
                    ],
                )
            )
        )
        self.source_file("c", self.repo)
        probe = scope._has_commit
        observed: list[tuple[object, ...]] = []

        def inspect(repo: str, commit: str) -> bool:
            observed.append(
                (
                    self.source("a") is not None,
                    self.source("b"),
                    self.source("c"),
                    self.need_source("old")["status"],
                    self.conn.execute(
                        "SELECT count(*) FROM scope_path WHERE cwd=?",
                        (str(self.tmp / "transient"),),
                    ).fetchone()[0],
                )
            )
            return probe(repo, commit)

        with mock.patch.object(scope, "_has_commit", inspect):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(observed, [(True, None, None, "active", 0)])
        self.assertEqual(
            (stats.files_changed, stats.missing, stats.failed), (3, 1, 0)
        )
        self.assertEqual(len(self.events("b")), 2)

    def test_full_retries_count_actual_commits_and_visits(self) -> None:
        other = self.tmp / "other"
        commit = self.make_repo(other, "other")
        scope.scope_id(self.conn, str(other))
        self.source_file("a", self.repo)
        self.source_file("b", self.gone, commit)
        self.source_file("c", self.repo)
        stats = ingest.run_pass(
            self.home, self.roots, full=True, fullfsync=False
        )
        self.assertEqual(
            (
                stats.files_seen,
                stats.files_changed,
                stats.events_added,
                stats.events_removed,
            ),
            (9, 5, 10, 4),
        )
        self.assertEqual(
            (stats.failed, stats.errors, stats.missing), (0, {}, 0)
        )
        self.assertEqual(self.row(self.conn, self.gone)[0], str(other))

    def test_replanning_reads_replaced_source_and_new_root(self) -> None:
        path = self.source_file("b", self.gone, self.commit)
        fresh = self.tmp / "fresh"
        fresh_commit = self.make_repo(fresh, "fresh")
        probe = scope._has_commit
        observations: list[tuple[str, str]] = []

        def mutate(repo: str, commit: str) -> bool:
            observations.append((repo, commit))
            answer = probe(repo, commit)
            if len(observations) == 1:
                path.unlink()
                self.source_file("b", self.gone, fresh_commit)
                self.source_file("a", fresh)
            return answer

        with mock.patch.object(scope, "_has_commit", mutate):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(
            observations,
            [
                (str(self.repo), self.commit),
                (str(self.repo), fresh_commit),
                (str(fresh), fresh_commit),
            ],
        )
        self.assertEqual(self.row(self.conn, self.gone)[0], str(fresh))
        self.assertEqual(
            (stats.files_seen, stats.files_changed, stats.failed), (7, 2, 0)
        )

    def test_append_during_preparation_is_read_in_new_pass(self) -> None:
        path = self.source_file("a", self.gone, self.commit)
        probe = scope._has_commit

        def append(repo: str, commit: str) -> bool:
            self.append(path, [user_msg(3, "arrived while unlocked")])
            return probe(repo, commit)

        with mock.patch.object(scope, "_has_commit", append):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual((stats.events_added, stats.files_seen), (3, 2))
        self.assertEqual(self.events("a")[-1][3], "arrived while unlocked")

    def test_serve_probes_unlocked_and_publishes_only_terminal_totals(
        self,
    ) -> None:
        self.source_file("a", self.gone, self.commit)
        obs.write_status(
            self.home, {"last_pass_at": 1.0, "last_error": "OSError"}
        )
        poller = cli_serve._Poller(self.home, {}, 60)
        poller.roots = self.roots
        probe = scope._has_commit
        observations: list[tuple[object, ...]] = []

        def inspect(repo: str, commit: str) -> bool:
            try:
                with store.writer_lock(self.home, wait_s=0):
                    free = True
            except store.BusyError:
                free = False
            status = obs.read_status(self.home)
            observations.append(
                (
                    free,
                    poller.conn,
                    poller.passes,
                    status["last_pass_at"],
                    status["last_error"],
                )
            )
            return probe(repo, commit)

        with mock.patch.object(scope, "_has_commit", inspect):
            poller.step()
        self.assertEqual(observations, [(True, None, 0, 1.0, "OSError")])
        status = obs.read_status(self.home)
        self.assertEqual(
            (status["files_seen"], status["passes"], status["failed"]),
            (2, 1, 0),
        )
        self.assertIsNone(status["last_error"])
        self.assertIsNone(poller.conn)

    def test_repeated_failures_then_recovery_publish_attempts(self) -> None:
        failing = self.source_file("a", self.repo)
        other = self.tmp / "other"
        hint = self.make_repo(other, "other")
        scope.scope_id(self.conn, str(other))
        self.source_file("b", self.gone, hint)
        opener = ingest_parse.open_regular
        attempts = 0
        unopened = self.source_file("c", self.repo)
        plan_opener = ingest_plan.open_regular
        opens = 0

        def failed_open(path: Path, **kwargs: Any) -> Any:
            nonlocal opens
            if path == unopened:
                opens += 1
                if opens <= 2:
                    raise PermissionError("synthetic")
            return plan_opener(path, **kwargs)

        def sometimes(path: Path, **kwargs: Any) -> Any:
            nonlocal attempts
            if path == failing:
                attempts += 1
                if attempts <= 2:
                    raise PermissionError("synthetic")
            return opener(path, **kwargs)

        poller = cli_serve._Poller(self.home, {}, 60)
        poller.roots = self.roots
        with (
            mock.patch.object(ingest_parse, "open_regular", sometimes),
            mock.patch.object(ingest_plan, "open_regular", failed_open),
        ):
            poller.step()
        self.assertEqual(len(self.events("a")), 2)
        status = obs.read_status(self.home)
        self.assertEqual(
            (status["failed"], status["errors"], status["files_changed"]),
            (2, {"PermissionError": 2}, 3),
        )
        notes = "\n".join(hook_notes.index_notes(self.home))
        self.assertEqual(
            (status["unreadable_files"], status["skipped_files"]), (2, 2)
        )
        self.assertEqual(len(self.events("c")), 2)
        self.assertIn("2 failed transcript open attempt(s)", notes)
        self.assertIn("2 failed source read attempt(s)", notes)
        self.assertIn("may include subsequently recovered sources", notes)
        self.assertNotIn("could not read 2 source(s)", notes)

    def test_stop_after_preparation_never_publishes_completed_pass(
        self,
    ) -> None:
        self.source_file("a", self.gone, self.commit)
        poller = cli_serve._Poller(self.home, {}, 60)
        poller.roots = self.roots
        probe = scope._has_commit

        def stop(repo: str, commit: str) -> bool:
            answer = probe(repo, commit)
            poller.stop_requested = True
            return answer

        with (
            mock.patch.object(scope, "_has_commit", stop),
            self.assertRaises(cli_serve._ServeStopError),
        ):
            poller.step()
        self.assertEqual(poller.passes, 0)
        self.assertIsNone(poller.conn)
        self.assertNotIn("last_pass_at", obs.read_status(self.home))
        self.assertIsNone(self.source("a"))

    def test_fatal_and_ordinary_errors_do_not_become_preparation(self) -> None:
        self.source_file("a", self.repo)
        with mock.patch.object(
            ingest_parse, "open_regular", side_effect=OSError("synthetic")
        ):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual((stats.failed, stats.errors), (1, {"OSError": 1}))
        with (
            mock.patch.object(
                ingest_parse,
                "open_regular",
                side_effect=TombstoneKeyError("synthetic"),
            ),
            self.assertRaises(TombstoneKeyError),
        ):
            ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertIsNone(self.source("a"))

    def test_cached_cwd_ignores_a_later_different_hint(self) -> None:
        self.conn.execute(
            "INSERT INTO scope_path(cwd,scope_id,method) VALUES(?,?,'git')",
            (str(self.gone), self.sid),
        )
        self.source_file("a", self.gone, "f" * 40)
        with mock.patch.object(
            scope,
            "_has_commit",
            side_effect=AssertionError("cached cwd must win"),
        ):
            stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual((stats.files_changed, stats.failed), (1, 0))
        self.assertEqual(self.row(self.conn, self.gone)[0], str(self.repo))

    def test_replay_and_tombstones_do_not_choose_the_cwd_mapping(self) -> None:
        other = self.tmp / "other"
        hint = self.make_repo(other, "other")
        scope.scope_id(self.conn, str(other))
        self.source_file("a", self.repo)
        self.write(
            "rollout-b.jsonl",
            [
                codex_meta(
                    "user",
                    "b",
                    cwd=str(self.gone),
                    forked_from_id="a",
                    git={"commit_hash": self.commit},
                ),
                user_msg(1, "a"),
                reply(2, "a answer"),
            ],
        )
        self.source_file("c", self.gone, self.commit)
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,created_at)"
            " VALUES('codex','thread','c',0)"
        )
        self.source_file("d", self.gone, hint)
        stats = ingest.run_pass(self.home, self.roots, fullfsync=False)
        self.assertEqual(self.events("b"), [])
        self.assertIsNone(self.source("c"))
        self.assertEqual(self.row(self.conn, self.gone)[0], str(other))
        self.assertEqual((stats.files_seen, stats.skipped_files), (12, 3))

    def test_busy_reacquisition_propagates_after_prior_work_commits(
        self,
    ) -> None:
        self.source_file("a", self.repo)
        self.source_file("b", self.gone, self.commit)
        probe = scope._has_commit
        held = store.writer_lock(self.home, wait_s=0)
        acquired = False

        def contend(repo: str, commit: str) -> bool:
            nonlocal acquired
            answer = probe(repo, commit)
            try:
                held.__enter__()
                acquired = True
            except store.BusyError:
                pass
            return answer

        try:
            with (
                mock.patch.object(scope, "_has_commit", contend),
                self.assertRaises(store.BusyError),
            ):
                ingest.run_pass(self.home, self.roots, fullfsync=False)
        finally:
            if acquired:
                held.__exit__(None, None, None)
        self.assertEqual(len(self.events("a")), 2)
        self.assertIsNone(self.source("b"))
