"""Actual fresh-process death preserves forward-recoverable exposure state."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing

from install import installer, record
from tests.legacy_cutover_support import crash
from tests.legacy_upgrade_support import BOOT_B, LegacyCase
from tests.test_review_shared_models import policy_data


class LegacyCrashTests(LegacyCase):
    """Kill the consumer process at durable exposure boundaries."""

    def check_cut(self, cut: str, expected_version: int) -> None:
        """Inspect bytes after process death, then recover in this process."""
        installer.install(self.ctx, self.world.repo, self.world.sha)
        done = crash(self.ctx, self.world.repo, self.world.sha, cut)
        self.assertEqual(done.returncode, 91, done.stderr.decode())
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                expected_version,
            )
        self.assertFalse(self.old.exists())
        self.observed_boot.return_value = BOOT_B
        installer.install(
            dataclasses.replace(self.ctx, ts="later"),
            self.world.repo,
            self.world.sha,
        )
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 2
            )
        self.assertTrue((self.ctx.rdir / "rollback-record.json").exists())
        self.assertEqual(self.summary()["outcome"], "ok")

    def test_crash_before_replace_does_not_promote_orphan(self) -> None:
        self.check_cut("before_publish", 3)
        self.assertEqual(
            len(list(self.ctx.data.glob("muninn.sqlite.recovery-*"))), 1
        )

    def test_crash_after_replace_recovers_actual_marked_store(self) -> None:
        self.check_cut("after_publish", 2)

    def test_crash_after_config_preserves_exact_completed_config(self) -> None:
        self.check_cut("after_configure", 2)

    def test_crash_after_launcher_exposure_uses_current_witness(self) -> None:
        self.check_cut("after_expose", 2)

    def test_crash_after_service_start_revalidates_registration(self) -> None:
        self.check_cut("after_start", 2)

    def test_crash_after_policy_commit_recognizes_exact_activation(
        self,
    ) -> None:
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.ctx.review_policy = self.ctx.home / "policy.json"
        self.ctx.review_policy.write_text(json.dumps(data))
        installer.install(self.ctx, self.world.repo, self.world.sha)
        done = crash(
            self.ctx, self.world.repo, self.world.sha, "after_policy_commit"
        )
        self.assertEqual(done.returncode, 91, done.stderr.decode())
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            before = conn.execute("SELECT * FROM review_control").fetchone()
        saved = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(saved["policy_activation"]["phase"], "intent")
        self.observed_boot.return_value = BOOT_B
        installer.install(
            dataclasses.replace(self.ctx, ts="later", review_policy=None),
            self.world.repo,
            self.world.sha,
        )
        with closing(sqlite3.connect(self.ctx.data / "muninn.sqlite")) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM review_control").fetchone(), before
            )
        saved = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(saved["policy_activation"]["phase"], "complete")
