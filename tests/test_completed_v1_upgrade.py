"""Qualify completed-owner repair before exact marked-one migration."""

from __future__ import annotations

import json
import os
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock

from install import cutover, recovery_guidance
from install.context import StepFailedError
from muninn import review_pause_state as state
from muninn import store, store_copy, tombstones
from tests.policy_preview_v1_support import V1PreviewFixture
from tests.test_review_completed_volume import BOOT, VOLUME
from tests.test_review_witness_compatibility import LegacyWitness


@unittest.skipIf(os.name == "nt", "macOS migration uses POSIX locks")
class CompletedV1UpgradeTests(V1PreviewFixture):
    """Keep the historical database unchanged until validated migration."""

    def setUp(self) -> None:
        super().setUp()
        for target, value in (
            (
                "muninn.review_pause_state.sys",
                SimpleNamespace(platform="darwin"),
            ),
            (
                "muninn.review_repair_binding.sys",
                SimpleNamespace(platform="darwin"),
            ),
            ("muninn.review_volume_identity.observe", VOLUME),
            ("muninn.review_boot_identity.observe", BOOT),
        ):
            patcher = (
                mock.patch(target, value)
                if target.endswith(".sys")
                else mock.patch(target, return_value=value)
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.old("state")
        fd = state.acquire(self.ctx.data)
        try:
            state.seed_new(self.ctx.data, fd, bind_volume=False)
            witness_path = self.ctx.data / state.WITNESS
            raw = json.loads(witness_path.read_bytes())
            raw["lock_identity"][0] += 4
            store.write_json_atomic(witness_path, raw)
            _, predecessor = state.inspect(self.ctx.data, state.WITNESS)
            state.publish(
                self.ctx.data,
                fd,
                state.BASELINE,
                state.Baseline(
                    version=1,
                    lock_identity=state.lock_identity(self.ctx.data, fd),
                    boot=BOOT,
                    predecessor=predecessor,
                    volume_uuid=VOLUME,
                ),
            )
        finally:
            os.close(fd)
        self.ctx.dry_run = False

    def test_confirmed_completed_repair_then_real_format_migration(
        self,
    ) -> None:
        original, rows = self.path.read_bytes(), self.rows()
        with mock.patch.object(
            recovery_guidance, "_confirm_legacy_volume", return_value=True
        ) as confirm:
            recovery_guidance.recover_pause(self.ctx, self.rec)
        confirm.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.rows(), rows)
        raw = (self.ctx.data / state.WITNESS).read_bytes()
        self.assertIsNotNone(state.parse(raw, LegacyWitness))
        self.assertFalse((self.ctx.data / state.BASELINE).exists())
        self.ctx.rdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with cutover.prepared_store(self.ctx, self.rec):
            pass
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(store_copy.validate_source(conn), 2)
        self.assertEqual(
            (self.ctx.data / self.rec["store"]["archive"]).read_bytes(),
            original,
        )

    def test_declined_or_noninteractive_confirmation_keeps_all_records(
        self,
    ) -> None:
        for answer in (False, None):
            with self.subTest(answer=answer):
                paths = (
                    self.path,
                    self.ctx.data / state.WITNESS,
                    self.ctx.data / state.BASELINE,
                )
                before = tuple(path.read_bytes() for path in paths)
                with (
                    mock.patch.object(
                        recovery_guidance,
                        "_confirm_legacy_volume",
                        return_value=answer,
                    ),
                    self.assertRaisesRegex(
                        StepFailedError, "marked_v1_recovery_required"
                    ),
                ):
                    recovery_guidance.recover_pause(self.ctx, self.rec)
                self.assertEqual(
                    tuple(path.read_bytes() for path in paths), before
                )

    def test_pending_historical_erasure_refuses_without_confirmation(
        self,
    ) -> None:
        tombstones.append_tombstones(
            self.ctx.data,
            [
                tombstones.tombstone_row(
                    "codex", "session", session_root="thread"
                )
            ],
        )
        original = self.path.read_bytes()
        witness = (self.ctx.data / state.WITNESS).read_bytes()
        with (
            mock.patch.object(
                recovery_guidance, "_confirm_legacy_volume"
            ) as confirm,
            self.assertRaisesRegex(
                StepFailedError, "marked_v1_recovery_required"
            ),
        ):
            recovery_guidance.recover_pause(self.ctx, self.rec)
        confirm.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.ctx.data / state.WITNESS).read_bytes(), witness)
        self.assertTrue((self.ctx.data / state.BASELINE).exists())

    def test_invalid_baseline_cannot_be_removed_by_owner_confirmation(
        self,
    ) -> None:
        baseline = self.ctx.data / state.BASELINE
        baseline.write_text("unverified")
        original = (self.ctx.data / state.WITNESS).read_bytes()
        with (
            mock.patch.object(
                recovery_guidance, "_confirm_legacy_volume"
            ) as confirm,
            self.assertRaisesRegex(
                StepFailedError, "marked_v1_recovery_required"
            ),
        ):
            recovery_guidance.recover_pause(self.ctx, self.rec)
        confirm.assert_not_called()
        self.assertEqual(baseline.read_text(), "unverified")
        self.assertEqual(
            (self.ctx.data / state.WITNESS).read_bytes(), original
        )
