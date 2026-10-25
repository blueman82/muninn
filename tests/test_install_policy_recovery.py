"""First-policy intents distinguish commit, rollback and fencing."""

from __future__ import annotations

import dataclasses
import json
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import (
    finish,
    installer,
    legacy_state,
    legacy_transition,
    record,
    review_policy,
)
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import review_pause as q
from muninn import store
from tests.legacy_upgrade_support import BOOT_B, LegacyCase
from tests.test_install_activation_lifetime import ActivationLifetimeTests
from tests.test_review_shared_models import policy_data


class PolicyRecoveryTests(ActivationLifetimeTests):
    """Exercise recovery using exact persisted activation records."""

    def test_failed_verify_allows_a_new_candidate_after_activation(
        self,
    ) -> None:
        with self.prepared():
            pass
        self.rec["failed"] = {"step": "verify", "error": "StepFailedError"}
        self.rec["recovery_required"] = True
        record.save(self.ctx, self.rec)
        with (
            mock.patch.object(review_policy, "verify_ready") as ready,
            legacy_state.operation(self.ctx) as check,
        ):
            self.assertIsNone(
                legacy_transition.resume_install(
                    self.ctx, self.world.repo, "f" * 40, check
                )
            )
        ready.assert_called_once_with(self.ctx, self.rec)
        finish.supersede(self.ctx, "f" * 40)
        saved = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(saved["superseded_by"], "f" * 40)
        self.assertNotIn("recovery_required", saved)
        self.assertIsNone(review_policy.pending(self.ctx))

    def test_commit_before_completion_save_is_recognized_without_reactivation(
        self,
    ) -> None:
        original = record.save

        def fail_completion(ctx: Ctx, rec: Record) -> None:
            if rec.get("policy_status") == "active":
                raise OSError("completion save")
            original(ctx, rec)

        with (
            mock.patch(
                "install.review_policy.record.save",
                side_effect=fail_completion,
            ),
            self.assertRaisesRegex(OSError, "completion save"),
            self.prepared(),
        ):
            pass
        committed = self.state()
        persisted = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(persisted["policy_activation"]["phase"], "intent")

        with (
            legacy_state.operation(self.ctx) as check,
            q.maintenance_owner(
                self.ctx.data, wait_s=0, status=q.EraseStatus()
            ) as owner,
        ):
            review_policy.activate(
                self.ctx, persisted, owner, store.db_path(self.ctx.data), check
            )
        self.assertEqual(self.state(), committed)
        self.assertEqual(persisted["policy_status"], "active")

    def test_committed_then_fenced_policy_stays_disabled(self) -> None:
        with self.prepared():
            pass
        with (
            store.writer_lock(self.ctx.data, wait_s=0),
            closing(store.connect_rw(store.db_path(self.ctx.data))) as conn,
        ):
            conn.execute(
                "UPDATE review_control SET admission=0,"
                "generation=generation+1"
            )
        before = self.state()

        with (
            legacy_state.operation(self.ctx) as check,
            q.maintenance_owner(
                self.ctx.data, wait_s=0, status=q.EraseStatus()
            ) as owner,
        ):
            review_policy.activate(
                self.ctx, self.rec, owner, store.db_path(self.ctx.data), check
            )
        self.assertEqual(self.state(), before)
        self.assertEqual(self.rec["policy_status"], "activation_incomplete")

    def test_installer_discovers_committed_activation_after_completion_failure(
        self,
    ) -> None:
        self.ctx.rdir.rename(self.ctx.rdir.with_name("unused-fixture-record"))
        original = record.save

        def fail_completion(ctx: Ctx, rec: Record) -> None:
            if rec.get("policy_status") == "active":
                raise OSError("completion save")
            original(ctx, rec)

        with (
            mock.patch(
                "install.review_policy.record.save",
                side_effect=fail_completion,
            ),
            self.assertRaisesRegex(OSError, "completion save"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        before = self.state()
        result = installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )
        self.assertEqual(self.state(), before)
        self.assertEqual(result["policy_status"], "active")

    def test_recovery_refuses_changed_python_selection_before_native_stop(
        self,
    ) -> None:
        self.ctx.rdir.rename(self.ctx.rdir.with_name("unused-fixture-record"))
        original = record.save

        def fail_completion(ctx: Ctx, rec: Record) -> None:
            if rec.get("policy_status") == "active":
                raise OSError("completion save")
            original(ctx, rec)

        with (
            mock.patch("install.review_policy.record.save", fail_completion),
            self.assertRaisesRegex(OSError, "completion save"),
        ):
            installer.install(self.ctx, self.world.repo, self.world.sha)
        selection = self.ctx.lib / "python"
        selection.unlink()
        selection.symlink_to("/different/python")
        with (
            mock.patch(
                "install.cutover.lifecycle.stop",
                side_effect=AssertionError("native stop attempted"),
            ),
            self.assertRaisesRegex(
                StepFailedError, "review_activation_runtime_changed"
            ),
        ):
            installer.install(
                dataclasses.replace(self.ctx, ts="later"),
                self.world.repo,
                self.world.sha,
            )


class LegacyActivationAuthorityTests(LegacyCase):
    """Retained configuration authority must survive the bootstrap commit."""

    def check_activation_authority_loss(self, kind: str, cut: str) -> None:
        """Refuse first-policy commit after runtime or config substitution."""
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.ctx.review_policy = self.ctx.home / "policy.json"
        self.ctx.review_policy.write_text(json.dumps(data))
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.observed_boot.return_value = BOOT_B
        original_ready = review_policy.ready
        original_save = record.save
        original_live = review_policy._check_live
        changed = False

        def mutate(ctx: Ctx, rec: Record) -> None:
            nonlocal changed
            changed = True
            path = (
                ctx.lib / rec["sha"] / "bin/muninn"
                if kind == "runtime"
                else ctx.config
            )
            raw = path.read_bytes() if path.exists() else b""
            path.write_bytes(raw + b"\n# substituted\n")

        def ready(ctx: Ctx, rec: Record) -> None:
            if cut == "ready":
                mutate(ctx, rec)
            original_ready(ctx, rec)

        def save(ctx: Ctx, rec: Record) -> None:
            original_save(ctx, rec)
            if (
                cut == "intent"
                and not changed
                and rec.get("policy_activation", {}).get("phase") == "intent"
            ):
                mutate(ctx, rec)

        def live(ctx: Ctx, rec: Record, path: Path) -> None:
            original_live(ctx, rec, path)
            if cut == "writer":
                mutate(ctx, rec)

        error = (
            "legacy_installation_changed"
            if kind == "runtime"
            else "legacy_configuration_changed"
        )
        with (
            mock.patch("install.review_policy.ready", side_effect=ready),
            mock.patch("install.review_policy._check_live", side_effect=live),
            mock.patch("install.record.save", side_effect=save),
            self.assertRaisesRegex(StepFailedError, error),
        ):
            installer.install(
                dataclasses.replace(self.ctx, ts="later"),
                self.world.repo,
                self.world.sha,
            )
        self.assertTrue(changed)
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT admission,policy_json,policy_digest,"
                        "activated_at "
                        "FROM review_control"
                    ).fetchone()
                ),
                (0, None, None, None),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_source").fetchone()[
                    0
                ],
                0,
            )
        rec = legacy_state.read(self.ctx.rdir / "rollback-record.json")
        if cut != "intent":
            self.assertFalse("policy_activation" in rec)
        self.assertNotEqual(
            rec.get("policy_activation", {}).get("phase"), "complete"
        )
        self.assertNotEqual(rec.get("policy_status"), "active")
        self.assertEqual(rec["legacy"]["phase"], "configured")
        self.assertFalse(self.world.fake.loaded)
        self.assertEqual(
            self.ctx.muninn.resolve(), self.ctx.rdir / "maintenance"
        )

    def test_changed_runtime_at_ready_cannot_activate(self) -> None:
        self.check_activation_authority_loss("runtime", "ready")

    def test_changed_config_at_ready_cannot_activate(self) -> None:
        self.check_activation_authority_loss("config", "ready")

    def test_changed_runtime_after_intent_cannot_commit(self) -> None:
        self.check_activation_authority_loss("runtime", "intent")

    def test_changed_config_after_intent_cannot_commit(self) -> None:
        self.check_activation_authority_loss("config", "intent")

    def test_changed_runtime_under_final_writer_cannot_bootstrap(self) -> None:
        self.check_activation_authority_loss("runtime", "writer")

    def test_changed_config_under_final_writer_cannot_bootstrap(self) -> None:
        self.check_activation_authority_loss("config", "writer")
