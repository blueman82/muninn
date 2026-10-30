"""Exercise both policy routes and early scope on populated old sources."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import (
    learning_scope,
    policy_preview,
    review_policy,
    review_policy_default,
)
from install.context import StepFailedError
from muninn import platform_lock, store
from tests.policy_preview_support import states
from tests.policy_preview_v1_support import V1PreviewFixture


class V1PolicyPreviewTests(V1PreviewFixture):
    """Catch target-format rejection before read-only upgrade preview."""

    def test_v2_check_accepts_frozen_v1_without_mutation(self) -> None:
        for shape in ("base", "state"):
            for explicit in (False, True):
                with self.subTest(shape=shape, explicit=explicit):
                    self.old(shape)
                    rows, files = self.rows(), states(self.home)
                    if explicit:
                        review_policy.preflight(self.ctx, self.rec)
                    else:
                        configured, retained = (
                            review_policy_default._configured(self.ctx)
                        )
                        self.assertFalse(configured)
                        self.assertEqual(retained, self.policy)
                    self.assertEqual(self.rows(), rows)
                    self.assertEqual(states(self.home), files)

    def test_real_preflight_accepts_source_without_migrating(self) -> None:
        for shape in ("base", "state"):
            with self.subTest(shape=shape):
                self.old(shape)
                self.ctx.dry_run = False
                rows, raw = self.rows(), self.path.read_bytes()
                policy_preview.validate(self.ctx, self.policy)
                self.assertEqual(
                    review_policy_default._configured(self.ctx),
                    (False, self.policy),
                )
                self.assertEqual(self.rows(), rows)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_old_paused_custom_policy_and_scope_are_retained(self) -> None:
        for shape in ("base", "state"):
            with self.subTest(shape=shape):
                self.old(shape, paused=True)
                self.ctx.dry_run = False
                self.policy = self.policy.model_copy(
                    update={
                        "policy_source": None,
                        "context_bytes": self.policy.context_bytes - 1,
                    }
                )
                from_raw = self.record_for(self.policy)["policy_input"]
                with closing(sqlite3.connect(self.path)) as conn:
                    conn.execute(
                        "UPDATE review_control SET "
                        "policy_json=?,policy_digest=?",
                        (from_raw["canonical"], from_raw["digest"]),
                    )
                    conn.commit()
                saved = self.selection()
                rows, files = self.rows(), states(self.home)
                prompt = mock.Mock(
                    side_effect=AssertionError("pause must remain")
                )
                learning_scope.choose(self.ctx, None, prompt)
                self.assertFalse(self.ctx.enable_learning)
                self.assertEqual(self.selection(), saved)
                self.assertEqual(self.rows(), rows)
                self.assertEqual(states(self.home), files)

    def test_altered_marker_ddl_and_fk_refuse_unchanged(self) -> None:
        for damage in (
            "DELETE FROM store_meta",
            "CREATE TABLE surprise(x)",
            "UPDATE review_input SET evidence_id='missing'",
            "UPDATE review_control SET policy_digest='bad'",
        ):
            for explicit in (False, True):
                with self.subTest(damage=damage, explicit=explicit):
                    self.old("state")
                    with closing(sqlite3.connect(self.path)) as conn:
                        conn.execute(damage)
                        conn.commit()
                    rows, files = self.rows(), states(self.home)
                    with self.assertRaises(StepFailedError):
                        if explicit:
                            review_policy.preflight(self.ctx, self.rec)
                        else:
                            review_policy_default._configured(self.ctx)
                    self.assertEqual(self.rows(), rows)
                    self.assertEqual(states(self.home), files)

    def test_ordinary_new_runtime_still_refuses_old_source(self) -> None:
        self.old("state")
        rows, raw = self.rows(), self.path.read_bytes()
        for opener in (store.connect_ro, store.connect_rw):
            with (
                self.assertRaisesRegex(
                    store.StoreUnavailableError, "^cutover_required$"
                ),
                closing(opener(self.path)),
            ):
                self.fail("old source opened by ordinary runtime")
            self.assertEqual(self.rows(), rows)
            self.assertEqual(self.path.read_bytes(), raw)

    def damage(self, condition: str) -> int | None:
        """Create one explicit refusal condition in the temporary source."""
        if condition == "missing":
            self.lock.unlink()
        elif condition == "held":
            descriptor = os.open(self.lock, os.O_RDWR)
            platform_lock.try_lock(descriptor)
            return descriptor
        elif condition == "public":
            self.lock.chmod(0o644)
        elif condition == "sidecar":
            sidecar = Path(str(self.path) + "-wal")
            sidecar.write_bytes(b"pending private state")
            sidecar.chmod(0o600)
        else:
            ceiling = self.policy.context_bytes - 1
            self.rec = self.record_for(
                self.policy.model_copy(update={"context_bytes": ceiling})
            )
        return None

    def test_both_old_shapes_retain_native_lock_and_sidecar_guards(
        self,
    ) -> None:
        for shape in ("base", "state"):
            for condition in (
                "missing",
                "held",
                "public",
                "sidecar",
                "incompatible",
            ):
                for explicit in (False, True):
                    with self.subTest(
                        shape=shape, condition=condition, explicit=explicit
                    ):
                        self.doCleanups()
                        self.setUp()
                        self.old(shape)
                        if condition == "public" and os.name == "nt":
                            continue
                        if condition == "incompatible" and not explicit:
                            continue
                        descriptor = self.damage(condition)
                        before, rows = states(self.home), self.rows()
                        try:
                            with self.assertRaises(StepFailedError):
                                if explicit:
                                    review_policy.preflight(self.ctx, self.rec)
                                else:
                                    review_policy_default._configured(self.ctx)
                            self.assertEqual(states(self.home), before)
                            self.assertEqual(self.rows(), rows)
                        finally:
                            if descriptor is not None:
                                os.close(descriptor)

    def test_old_saved_now_and_all_scope_are_retained_without_reselection(
        self,
    ) -> None:
        for mode in ("now", "all"):
            self.old("state")
            with closing(sqlite3.connect(self.path)) as conn:
                conn.execute("UPDATE learning_setup SET mode=?", (mode,))
                conn.commit()
            self.ctx.dry_run = False
            self.ctx.learning_mode = None
            self.ctx.retained_learning_scope = False
            before, saved = states(self.home), self.selection()
            learning_scope.choose(
                self.ctx, None, mock.Mock(side_effect=AssertionError)
            )
            self.assertTrue(self.ctx.retained_learning_scope)
            self.assertEqual(self.ctx.learning_mode, mode)
            self.assertEqual(self.selection(), saved)
            self.assertEqual(states(self.home), before)
