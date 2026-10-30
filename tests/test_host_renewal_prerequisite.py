"""Qualify the separate ordinary marked-v1 upgrade before host renewal."""

from __future__ import annotations

import dataclasses
import io
import json
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from install import decision_migration, installer, lifecycle
from install.context import Ctx
from install.record import Record
from muninn import review_pause as q
from muninn import review_recovery, review_requests, review_supervisor, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tests.host_renewal_inventory import IndexedSource
from tests.host_renewal_prerequisite_support import PrerequisiteFixture


class HostRenewalPrerequisiteTests(PrerequisiteFixture):
    """Preserve saved authority through the established historical upgrade."""

    def test_ordinary_v1_upgrade_retains_mismatched_host_policy(self) -> None:
        for mode in ("now", "all"):
            for paused in (False, True):
                with self.subTest(mode=mode, paused=paused):
                    if mode != "now" or paused:
                        self.doCleanups()
                        self.setUp()
                    self.ordinary_v1(mode, paused)

    def ordinary_v1(self, mode: str, paused: bool) -> None:
        """Run the unchanged installer over independently frozen old DDL."""
        old, source = self.prepare_v1(mode)
        self.old = old
        with closing(sqlite3.connect(self.path)) as conn:
            before = conn.execute(
                "SELECT policy_json,policy_digest,activated_at FROM "
                "review_control"
            ).fetchone()
        unfinished = self.request(number=2)
        self.attempt(unfinished, number=2)
        with closing(sqlite3.connect(self.path)) as current:
            snapshots = tuple(
                current.execute(
                    "SELECT id,policy_json,policy_digest "
                    "FROM review_request "
                    "ORDER BY id"
                )
            )
            progress = tuple(current.execute("SELECT * FROM review_source"))
        artifacts = {
            str(path.relative_to(self.ctx.data)): path.read_bytes()
            for directory in ("review-capabilities", "review-prompts")
            for path in (self.ctx.data / directory).rglob("*.json")
        }
        schema = (self.ctx.data / "review-codex-schema.json").read_bytes()
        self.frozen_runtime_accepts_old()
        observed = self.run_ordinary(paused)
        self.assert_preserved(mode, before, snapshots, progress, observed)
        self.assertEqual(observed, [0 if paused else 1])
        self.assertEqual(
            artifacts,
            {
                str(path.relative_to(self.ctx.data)): path.read_bytes()
                for directory in ("review-capabilities", "review-prompts")
                for path in (self.ctx.data / directory).rglob("*.json")
            },
        )
        self.assertEqual(
            schema, (self.ctx.data / "review-codex-schema.json").read_bytes()
        )
        self.actual_host_guard(old, source, paused)

    def actual_host_guard(
        self, policy: ReviewPolicy, source: IndexedSource, paused: bool
    ) -> None:
        """Reach the real supervisor guard before reservation."""
        if not paused:
            with (
                store.writer_lock(self.ctx.data),
                closing(store.connect_rw(self.path)) as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
                review_requests.enqueue_review(
                    conn,
                    source.window(2, 2),
                    source.scope,
                    policy,
                    "explicit",
                    30.0,
                )
                conn.execute("COMMIT")
        supervisor = review_supervisor.Supervisor(self.ctx.data, policy)
        with (
            mock.patch.object(
                review_supervisor,
                "reserve_call",
                side_effect=AssertionError("reserved"),
            ) as reserve,
            mock.patch.object(
                review_supervisor,
                "create_wrapper",
                side_effect=AssertionError("launched"),
            ) as launch,
        ):
            if paused:
                with self.assertRaisesRegex(
                    ValueError, "review_policy_required"
                ):
                    supervisor.start(31.0)
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM review_attempt"
                        ).fetchone()[0],
                        2,
                    )
                reserve.assert_not_called()
                launch.assert_not_called()
                return
            supervisor.start(31.0)
            try:
                for now in (32.0, 93.0, 154.0):
                    supervisor.tick(now)
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM review_call"
                        ).fetchone()[0],
                        0,
                    )
                    notice = review_recovery.notice(conn)
                    assert notice is not None
                    self.assertEqual(notice["code"], "codex_host_changed")
            finally:
                supervisor.stop(33.0)
        reserve.assert_not_called()
        launch.assert_not_called()

    def run_ordinary(
        self, paused: bool, *, scope: str | None = None, expected: int = 0
    ) -> list[int]:
        """Run real orchestration with an observed fake service shutdown."""
        observed: list[int] = []
        original_stop, original_migrate = (
            lifecycle.stop,
            decision_migration.migrate_decisions,
        )

        def stop(active: Ctx) -> None:
            original_stop(active)
            with closing(
                sqlite3.connect(self.path, isolation_level=None)
            ) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    return
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    (
                        "UPDATE review_control SET "
                        "generation=generation+1,admission=?"
                    ),
                    (0 if paused else 1,),
                )
                generation = conn.execute(
                    "SELECT generation FROM review_control"
                ).fetchone()[0]
                review_requests.recover_attempts(conn, generation, 10.0)
                conn.execute("COMMIT")

        def migrate(
            active: Ctx, rec: Record, owner: q.MaintenanceOwner
        ) -> Path:
            with closing(sqlite3.connect(self.path)) as conn:
                observed.append(
                    conn.execute(
                        "SELECT admission FROM review_control"
                    ).fetchone()[0]
                )
            return original_migrate(active, rec, owner)

        ctx = dataclasses.replace(
            self.ctx, dry_run=False, review_policy=None, default_home=True
        )
        with (
            mock.patch.object(installer, "Ctx", return_value=ctx),
            mock.patch.object(lifecycle, "stop", stop),
            mock.patch.object(
                decision_migration, "migrate_decisions", migrate
            ),
            mock.patch("builtins.input", return_value="no") as prompt,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = installer.main(
                ["--repo", str(self.repo), "--sha", self.sha, "--upgrade"]
                + ([] if scope is None else ["--learning-scope", scope])
            )
        self.assertEqual(result, expected, output.getvalue())
        if scope == "all":
            self.assertIn("history_not_confirmed", output.getvalue())
            self.assertTrue(
                any(
                    "Review these verified records?" in str(call)
                    for call in prompt.call_args_list
                )
            )
        self.assertFalse(
            any("host" in str(call) for call in prompt.call_args_list)
        )
        self.assertNotIn(
            "host_renewal",
            json.loads((ctx.lib / "install-record.json").read_text()),
        )
        return observed

    def assert_preserved(
        self,
        mode: str,
        before: tuple[object, ...],
        snapshots: tuple[tuple[object, ...], ...],
        progress: tuple[tuple[object, ...], ...],
        observed: list[int],
    ) -> None:
        """Compare authority and progress against direct observations."""
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 2
            )
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT policy_json,policy_digest,activated_at FROM "
                        "review_control"
                    ).fetchone()
                ),
                before,
            )
            self.assertEqual(
                tuple(
                    tuple(row)
                    for row in conn.execute(
                        "SELECT id,policy_json,policy_digest "
                        "FROM review_request "
                        "ORDER BY id"
                    )
                ),
                snapshots,
            )
            selection = dict(
                conn.execute("SELECT * FROM learning_setup").fetchone()
            )
            self.assertEqual(selection["mode"], mode)
            self.assertEqual(
                tuple(
                    tuple(row)
                    for row in conn.execute("SELECT * FROM review_source")
                ),
                progress,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT admission FROM review_control"
                ).fetchone()[0],
                observed[-1],
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                len(snapshots),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_call").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT outcome FROM review_attempt WHERE id=?",
                    ("A_" + "2" * 32,),
                ).fetchone()[0],
                "abandoned",
            )
            if mode == "now":
                self.assertEqual(selection["selected_at"], 1.5)

    def test_retained_all_does_not_request_unselected_history(self) -> None:
        for mode, prepared in (
            ("all", False),
            ("all", True),
            ("now", False),
            ("now", True),
        ):
            with self.subTest(mode=mode, prepared=prepared):
                self.doCleanups()
                self.setUp()
                old, _ = self.prepare_v1(mode)
                self.sql(
                    "UPDATE review_request SET from_line=1,through_line=1"
                )
                if prepared:
                    self.world.fake.doctor = 1
                    self.run_ordinary(False, expected=1)
                    self.world.fake.doctor = 0
                    self.ctx = dataclasses.replace(
                        self.ctx, ts="20261003T000000Z"
                    )
                with closing(sqlite3.connect(self.path)) as conn:
                    selection = tuple(
                        conn.execute("SELECT * FROM learning_setup").fetchone()
                    )
                    progress = tuple(
                        conn.execute("SELECT * FROM review_source")
                    )
                    requests = tuple(
                        conn.execute(
                            "SELECT id,policy_json,policy_digest "
                            "FROM review_request"
                        )
                    )
                self.run_ordinary(False)
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertEqual(
                        tuple(
                            conn.execute(
                                "SELECT * FROM learning_setup"
                            ).fetchone()
                        ),
                        selection,
                    )
                    self.assertEqual(
                        tuple(
                            tuple(row)
                            for row in conn.execute(
                                "SELECT * FROM review_source"
                            )
                        ),
                        progress,
                    )
                    self.assertEqual(
                        tuple(
                            tuple(row)
                            for row in conn.execute(
                                "SELECT id,policy_json,policy_digest "
                                "FROM review_request"
                            )
                        ),
                        requests,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT policy_json FROM review_control"
                        ).fetchone()[0],
                        canonical_json(old),
                    )

    def test_explicit_scope_still_requires_history_consent(self) -> None:
        self.prepare_v1("all")
        self.sql("UPDATE review_request SET from_line=1,through_line=1")
        self.run_ordinary(False, scope="all", expected=1)
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_call").fetchone()[0],
                0,
            )
