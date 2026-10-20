"""Explicit installer policy input is strict, frozen and value-free."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import json
import os
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock

from install import installer, legacy_baseline
from install.context import StepFailedError
from muninn import store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tests.installer_support import World
from tests.test_review_shared_models import policy_data
from tools.prepare_codex_policy import policy


class InstallPolicyTests(unittest.TestCase):
    """Exercise policy intake without native installation effects."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        self.path = self.world.home / "policy.json"
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.raw = json.dumps(data).encode()
        self.path.write_bytes(self.raw)

    def intake(self) -> dict[str, object]:
        """Call the installer boundary after requiring its implementation."""
        self.assertIsNotNone(importlib.util.find_spec("install.review_policy"))
        module = importlib.import_module("install.review_policy")
        return module.capture(self.ctx, self.world.sha)

    def test_shared_file_snapshot_accepts_windows_descriptor_metadata(
        self,
    ) -> None:
        """Windows path and descriptor views differ in mode and ctime."""
        original_fstat = os.fstat

        def descriptor_state(fd: int) -> SimpleNamespace:
            found = original_fstat(fd)
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            values = {name: getattr(found, name) for name in fields}
            values["st_mode"] = self.path.lstat().st_mode ^ 0o111
            values["st_ctime_ns"] += 1
            return SimpleNamespace(**values)

        with (
            mock.patch.object(
                legacy_baseline, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(legacy_baseline.os, "fstat", descriptor_state),
            mock.patch.object(
                legacy_baseline.platform_io, "is_private", return_value=True
            ),
        ):
            before = legacy_baseline.file_state(self.path, private=True)
        self.assertEqual(
            before["sha256"], hashlib.sha256(self.raw).hexdigest()
        )

        reads = 0

        def changing_descriptor(fd: int) -> SimpleNamespace:
            nonlocal reads
            reads += 1
            found = descriptor_state(fd)
            if reads == 3:
                found.st_mode ^= 0o111
            return found

        with (
            mock.patch.object(
                legacy_baseline, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(
                legacy_baseline.os, "fstat", changing_descriptor
            ),
            mock.patch.object(
                legacy_baseline.platform_io, "is_private", return_value=True
            ),
            self.assertRaisesRegex(StepFailedError, "legacy_file_changed"),
        ):
            legacy_baseline.file_state(self.path, private=True)

    def test_parser_carries_explicit_path_without_automatic_discovery(
        self,
    ) -> None:
        args = installer._parser().parse_args(
            [
                "--repo",
                ".",
                "--sha",
                "a" * 40,
                "--fresh",
                "--review-policy",
                str(self.path),
            ]
        )
        self.assertEqual(args.review_policy, self.path)
        self.assertEqual(self.intake()["status"], "activation_pending")

    def test_raw_and_canonical_snapshot_survive_external_file_change(
        self,
    ) -> None:
        self.assertIn("review_policy", self.ctx.__dataclass_fields__)
        self.ctx.review_policy = self.path
        captured = self.intake()
        self.assertEqual(captured["raw"], self.raw.decode())
        self.path.write_text("{}")
        module = importlib.import_module("install.review_policy")
        rec = {"sha": self.world.sha, "policy_input": captured}
        self.ctx.review_policy = None
        self.assertIsNotNone(module.frozen(self.ctx, rec))
        self.ctx.review_policy = self.path
        with self.assertRaises(StepFailedError):
            module.frozen(self.ctx, rec, check_supplied=True)
        self.assertFalse(self.ctx.data.exists())

    def test_generated_ongoing_policy_survives_intake_and_activation(
        self,
    ) -> None:
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
        ):
            selected, _ = policy(
                self.ctx.data,
                self.world.home / "synthetic-codex",
                "synthetic-fixture-owner",
                1.0,
            )
            raw = canonical_json(selected)
            self.path.write_text(raw)
            self.ctx.review_policy = self.path
            captured = self.intake()
            self.assertEqual(captured["raw"], raw)
            module = importlib.import_module("install.review_policy")
            self.assertEqual(
                module.frozen(
                    self.ctx,
                    {"sha": self.world.sha, "policy_input": captured},
                    check_supplied=True,
                ),
                selected,
            )
            installer.install(self.ctx, self.world.repo, self.world.sha)
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            control = conn.execute("SELECT * FROM review_control").fetchone()
            self.assertEqual(control["admission"], 1)
            self.assertEqual(
                ReviewPolicy.model_validate_json(control["policy_json"]),
                selected,
            )

    def test_invalid_duplicate_nonfinite_and_linked_input_refuse(self) -> None:
        self.assertIn("review_policy", self.ctx.__dataclass_fields__)
        self.ctx.review_policy = self.path
        for raw in (
            b"{}",
            b'{"max_rounds":1,"max_rounds":2}',
            b'{"x":NaN}',
            b"\xff",
            b"x" * (1024 * 1024 + 1),
        ):
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                with self.assertRaises(StepFailedError):
                    self.intake()
        self.path.unlink()
        target = self.path.with_suffix(".target")
        target.write_bytes(self.raw)
        self.path.symlink_to(target)
        with self.assertRaises(StepFailedError):
            self.intake()
        self.assertFalse(self.ctx.data.exists())

    def test_partial_frozen_policy_snapshot_refuses_without_external_input(
        self,
    ) -> None:
        self.ctx.review_policy = self.path
        value = self.intake()
        self.ctx.review_policy = None
        module = importlib.import_module("install.review_policy")
        for field in ("raw", "raw_digest", "canonical", "digest"):
            for null in (False, True):
                with self.subTest(field=field, null=null):
                    damaged = dict(value)
                    if null:
                        damaged[field] = None
                    else:
                        damaged.pop(field)
                    with self.assertRaisesRegex(
                        StepFailedError, "review_policy_snapshot_changed"
                    ):
                        module.frozen(
                            self.ctx,
                            {"sha": self.world.sha, "policy_input": damaged},
                            check_supplied=True,
                        )
        self.assertIsNone(
            module.frozen(
                self.ctx,
                {"sha": self.world.sha, "policy_input": self.intake()},
                check_supplied=True,
            )
        )


class ConfiguredPolicyTests(unittest.TestCase):
    """Reject replacements before any supervisor-stop acquisition."""

    def setUp(self) -> None:
        self.world = World(self)
        self.ctx = self.world.ctx(fresh=True)
        self.path = self.ctx.home / "policy.json"
        self.data = policy_data()
        self.data["projection_version"] = "permitted-fields-v2"
        self.path.write_text(json.dumps(self.data))
        self.ctx.review_policy = self.path
        installer.install(self.ctx, self.world.repo, self.world.sha)
        self.later = dataclasses.replace(
            self.ctx, fresh=False, upgrade=True, ts="later"
        )

    def test_different_policy_refuses_without_requesting_runtime_stop(
        self,
    ) -> None:
        self.data["policy_id"] = "different"
        self.path.write_text(json.dumps(self.data))
        with (
            mock.patch(
                "install.review_policy.q.maintenance_owner",
                side_effect=AssertionError("runtime stop attempted"),
            ),
            self.assertRaisesRegex(
                StepFailedError, "review_policy_transition_required"
            ),
        ):
            installer.install(self.later, self.world.repo, self.world.sha)

    def test_identical_policy_preserves_disabled_admission_and_activation_time(
        self,
    ) -> None:
        with (
            store.writer_lock(self.ctx.data, wait_s=0),
            closing(store.connect_rw(store.db_path(self.ctx.data))) as conn,
        ):
            conn.execute("UPDATE review_control SET admission=0")
            before = tuple(
                conn.execute(
                    "SELECT admission,policy_json,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )
        installer.install(self.later, self.world.repo, self.world.sha)
        with closing(store.connect_ro(store.db_path(self.ctx.data))) as conn:
            after = tuple(
                conn.execute(
                    "SELECT admission,policy_json,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )
        self.assertEqual(after, before)
