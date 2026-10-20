"""Installer normal exit alone bootstraps policy under retained ownership."""

from __future__ import annotations

import dataclasses
import json
import os
import unittest
from collections.abc import Generator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest import mock

from install import cutover, installer, legacy_state, record, review_policy
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_lock, store
from muninn import review_quiescence as q
from tests.installer_support import World
from tests.test_review_shared_models import policy_data


class ActivationLifetimeTests(unittest.TestCase):
    """Use real governed stores with fake native effects only."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        path = self.ctx.home / "policy.json"
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        path.write_text(json.dumps(data))
        self.ctx.review_policy = path
        self.rec = installer.preflight(
            self.ctx, self.world.repo, self.world.sha
        )
        installer.record(self.ctx, self.rec)

    def state(self) -> tuple[object, ...]:
        """Observe admission and immutable first-policy state."""
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            return tuple(
                conn.execute(
                    "SELECT admission,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )

    @contextmanager
    def prepared(self) -> Generator[Path]:
        """Retain the real installer lock around its activation context."""
        self.ctx.lib.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            legacy_state.operation(self.ctx) as check,
            cutover.prepared_install_store(self.ctx, self.rec, check) as path,
        ):
            yield path

    def test_normal_close_activates_after_yield_and_releases_roots(
        self,
    ) -> None:
        with ExitStack() as stack:
            stack.enter_context(self.prepared())
            self.assertEqual(self.state(), (0, None, None))
            with (
                store.writer_lock(self.ctx.data, wait_s=0),
                closing(
                    store.connect_rw(store.db_path(self.ctx.data))
                ) as conn,
            ):
                conn.execute(
                    "INSERT INTO scope VALUES(5,'g','Global','global')"
                )
            stack.close()
            self.assertEqual(self.state()[0], 1)
        self.assertEqual(self.rec["policy_status"], "active")

    def test_exception_unwind_does_not_activate(self) -> None:
        with (
            self.assertRaisesRegex(RuntimeError, "configuration failed"),
            ExitStack() as stack,
        ):
            stack.enter_context(self.prepared())
            raise RuntimeError("configuration failed")
        self.assertEqual(self.state(), (0, None, None))

    def test_standalone_preparation_with_policy_does_not_activate(
        self,
    ) -> None:
        cutover.prepare_store(self.ctx, self.rec)
        self.assertEqual(self.state(), (0, None, None))

    def test_failed_activation_record_rolls_back_first_policy(self) -> None:
        stack = ExitStack()
        stack.enter_context(self.prepared())
        with (
            mock.patch(
                "install.review_policy.record.save",
                side_effect=OSError("record failed"),
            ),
            self.assertRaisesRegex(OSError, "record failed"),
        ):
            stack.close()
        self.assertEqual(self.state(), (0, None, None))

    def test_fresh_config_finishes_before_service_start(self) -> None:
        order = [step.__name__ for step in installer.FRESH_STEPS]
        self.assertLess(order.index("cursor"), order.index("start_new"))

    def test_windows_operation_uses_descriptor_sync_without_directory_sync(
        self,
    ) -> None:
        self.ctx.lib.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            mock.patch.object(legacy_state.os, "name", "nt"),
            mock.patch.object(
                legacy_state.platform_io, "is_private", return_value=True
            ),
            mock.patch.object(legacy_state.file_sync, "sync_fd") as sync,
            mock.patch.object(
                legacy_state.file_sync,
                "sync_path",
                side_effect=OSError("unsupported directory sync"),
            ),
            legacy_state.operation(self.ctx) as check,
        ):
            self.assertEqual(len(check()), 2)
            sync.assert_called_once()

    def test_fresh_installer_retains_operation_lock_through_activation(
        self,
    ) -> None:
        self.ctx.rdir.rename(self.ctx.rdir.with_name("unused-fixture-record"))
        original = cutover.review_policy.ready

        def ready(ctx: installer.Ctx, rec: installer.Record) -> None:
            with (
                self.assertRaises(BlockingIOError),
                legacy_state.operation(ctx),
            ):
                self.fail("fresh operation lock was released")
            original(ctx, rec)

        with mock.patch(
            "install.cutover.review_policy.ready", side_effect=ready
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)

    def test_ordinary_record_still_refuses_existing_snapshot_directory(
        self,
    ) -> None:
        before = (self.ctx.rdir / "rollback-record.json").read_bytes()
        with self.assertRaises(FileExistsError):
            installer.record(self.ctx, self.rec)
        self.assertEqual(
            (self.ctx.rdir / "rollback-record.json").read_bytes(), before
        )

    def replace_operation_lock(self) -> None:
        """Replace the pathname while the installer retains the old inode."""
        lock = self.ctx.lib / ".install-record.lock"
        lock.rename(lock.with_name("displaced-operation-lock"))
        os.close(platform_lock.acquire(lock))

    def check_pre_activation_lock_loss(self, upgrade: bool) -> None:
        """Keep admission disabled and service stopped after authority loss."""
        self.ctx.rdir.rename(self.ctx.rdir.with_name("unused-fixture-record"))
        if upgrade:
            installer.install(
                dataclasses.replace(self.ctx, review_policy=None),
                self.world.repo,
                self.world.sha,
            )
            self.ctx = dataclasses.replace(
                self.ctx, ts="upgrade", fresh=False, upgrade=True
            )
        original = cutover.review_policy.ready

        def ready(ctx: installer.Ctx, rec: installer.Record) -> None:
            original(ctx, rec)
            self.replace_operation_lock()

        with (
            mock.patch("install.cutover.review_policy.ready", ready),
            self.assertRaisesRegex(Exception, "install_lock_changed"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertEqual(self.state(), (0, None, None))
        self.assertFalse(self.world.fake.loaded)

    def test_fresh_lock_loss_before_activation_cannot_enable_or_start(
        self,
    ) -> None:
        self.check_pre_activation_lock_loss(False)

    def test_current_lock_loss_before_activation_cannot_enable_or_start(
        self,
    ) -> None:
        self.check_pre_activation_lock_loss(True)

    def test_lock_loss_after_activation_blocks_service_start(self) -> None:
        self.ctx.rdir.rename(self.ctx.rdir.with_name("unused-fixture-record"))
        original = cutover.review_policy.activate

        def activate(
            ctx: installer.Ctx,
            rec: installer.Record,
            owner: q.MaintenanceOwner,
            path: Path,
            check: legacy_state.OperationCheck,
        ) -> None:
            original(ctx, rec, owner, path, check)
            self.replace_operation_lock()

        with (
            mock.patch("install.cutover.review_policy.activate", activate),
            self.assertRaisesRegex(Exception, "install_lock_changed"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertFalse(self.world.fake.loaded)


class OrdinaryActivationAuthorityTests(unittest.TestCase):
    """Frozen ordinary configuration remains authoritative through commit."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.policy = self.ctx.home / "policy.json"
        self.policy.write_text(json.dumps(data))

    def mutate(self, ctx: Ctx, rec: Record, kind: str) -> None:
        """Substitute bytes after the installer freezes their authority."""
        path = (
            ctx.lib / rec["sha"] / "bin/muninn"
            if kind == "runtime"
            else ctx.config
        )
        raw = path.read_bytes() if path.exists() else b""
        path.write_bytes(raw + b"\n# substituted after capture\n")

    def assert_not_activated(self) -> None:
        """Require null policy, stopped service and no successful summary."""
        home = (
            self.ctx.data
            if self.ctx.data.exists()
            else self.ctx.failed / "data"
        )
        with closing(store.connect_ro(store.db_path(home))) as conn:
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT admission,policy_digest,activated_at "
                        "FROM review_control"
                    ).fetchone()
                ),
                (0, None, None),
            )
        self.assertFalse(self.world.fake.loaded)
        summary = self.ctx.lib / "install-record.json"
        if summary.exists():
            self.assertNotEqual(
                json.loads(summary.read_bytes()).get("outcome"), "ok"
            )

    def check_install_cut(self, upgrade: bool, kind: str, cut: str) -> None:
        """Refuse first activation after ready or durable-intent changes."""
        if upgrade:
            installer.install(self.ctx, self.world.repo, self.world.sha)
            self.ctx = dataclasses.replace(
                self.ctx, fresh=False, upgrade=True, ts="upgrade"
            )
        self.ctx.review_policy = self.policy
        original_ready, original_save = review_policy.ready, record.save
        changed = False

        def ready(ctx: Ctx, rec: Record) -> None:
            nonlocal changed
            original_ready(ctx, rec)
            if cut == "ready":
                self.mutate(ctx, rec, kind)
                changed = True

        def save(ctx: Ctx, rec: Record) -> None:
            nonlocal changed
            original_save(ctx, rec)
            if (
                cut == "intent"
                and not changed
                and rec.get("policy_activation", {}).get("phase") == "intent"
            ):
                self.mutate(ctx, rec, kind)
                changed = True

        error = "runtime" if kind == "runtime" else "configuration"
        with (
            mock.patch("install.review_policy.ready", side_effect=ready),
            mock.patch("install.record.save", side_effect=save),
            self.assertRaisesRegex(
                StepFailedError, f"review_activation_{error}_changed"
            ),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assertTrue(changed)
        self.assert_not_activated()

    def test_fresh_runtime_after_ready_refuses(self) -> None:
        self.check_install_cut(False, "runtime", "ready")

    def test_fresh_config_after_ready_refuses(self) -> None:
        self.check_install_cut(False, "config", "ready")

    def test_fresh_runtime_after_intent_refuses(self) -> None:
        self.check_install_cut(False, "runtime", "intent")

    def test_fresh_config_after_intent_refuses(self) -> None:
        self.check_install_cut(False, "config", "intent")

    def test_current_runtime_after_ready_refuses(self) -> None:
        self.check_install_cut(True, "runtime", "ready")

    def test_current_config_after_ready_refuses(self) -> None:
        self.check_install_cut(True, "config", "ready")

    def test_current_runtime_after_intent_refuses(self) -> None:
        self.check_install_cut(True, "runtime", "intent")

    def test_current_config_after_intent_refuses(self) -> None:
        self.check_install_cut(True, "config", "intent")

    def check_resume_cut(self, kind: str, cut: str) -> None:
        """Refuse a rolled-back bootstrap after stop or pre-COMMIT changes."""
        self.ctx.review_policy = self.policy
        original_save = record.save

        def fail_intent(ctx: Ctx, rec: Record) -> None:
            original_save(ctx, rec)
            if rec.get("policy_activation", {}).get("phase") == "intent":
                raise OSError("saved intent fault")

        with (
            mock.patch("install.record.save", side_effect=fail_intent),
            self.assertRaisesRegex(OSError, "saved intent fault"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        self.assert_not_activated()
        original_stop = cutover.lifecycle.stop
        changed = False

        def stop(ctx: Ctx) -> None:
            nonlocal changed
            original_stop(ctx)
            if cut == "stop":
                self.mutate(ctx, {"sha": self.world.sha}, kind)
                changed = True

        def save(ctx: Ctx, rec: Record) -> None:
            nonlocal changed
            original_save(ctx, rec)
            if (
                cut == "intent"
                and not changed
                and rec.get("policy_activation", {}).get("phase") == "intent"
            ):
                self.mutate(ctx, rec, kind)
                changed = True

        error = "runtime" if kind == "runtime" else "configuration"
        with (
            mock.patch("install.cutover.lifecycle.stop", side_effect=stop),
            mock.patch("install.record.save", side_effect=save),
            self.assertRaisesRegex(
                StepFailedError, f"review_activation_{error}_changed"
            ),
        ):
            installer.install(
                dataclasses.replace(self.ctx, ts="resume"),
                self.world.repo,
                self.world.sha,
            )
        self.assertTrue(changed)
        self.assert_not_activated()

    def test_resume_runtime_after_stop_refuses(self) -> None:
        self.check_resume_cut("runtime", "stop")

    def test_resume_config_after_stop_refuses(self) -> None:
        self.check_resume_cut("config", "stop")

    def test_resume_runtime_after_intent_refuses(self) -> None:
        self.check_resume_cut("runtime", "intent")

    def test_resume_config_after_intent_refuses(self) -> None:
        self.check_resume_cut("config", "intent")
