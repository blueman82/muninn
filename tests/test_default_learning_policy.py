"""Exercise the bundled policy's install and safe refresh boundaries."""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import unittest
from contextlib import closing
from unittest import mock

from install import installer
from install.context import StepFailedError
from muninn import store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tests.installer_support import World
from tests.test_review_shared_models import policy_data
from tools.prepare_codex_policy import policy


class DefaultLearningPolicyTests(unittest.TestCase):
    """Keep repository-default selection and refresh within their bounds."""

    @unittest.skipIf(
        sys.platform == "win32",
        "Default Codex capability files require POSIX private-file checks.",
    )
    def test_fresh_install_activates_the_bound_repository_policy(self) -> None:
        """Create the packaged capability after setup on a supported host."""
        world = World(self)
        ctx = world.ctx(fresh=True, default_home=True)
        executable = world.home / "codex"
        executable.write_text("signed Codex fixture")
        executable.chmod(0o700)
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("shutil.which", return_value=str(executable)),
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(executable, "1" * 64),
            ),
        ):
            result = installer.install(ctx, world.repo, world.sha)
        with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
            row = conn.execute(
                "SELECT admission,policy_json FROM review_control"
            ).fetchone()
        selected = ReviewPolicy.model_validate_json(row["policy_json"])
        self.assertEqual(result["policy_status"], "active")
        self.assertEqual(row["admission"], 1)
        self.assertEqual(selected.policy_source, "repository_default")
        self.assertEqual(selected.max_input_bytes, 1_048_576)

    def test_unsupported_host_keeps_policy_pending(self) -> None:
        """Leave admission unconfigured when native host checks fail."""
        world = World(self)
        ctx = world.ctx(fresh=True, default_home=True)
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="x86_64"),
        ):
            module = importlib.import_module("install.review_policy")
            captured = module.capture(ctx, world.sha)
        self.assertEqual(captured["status"], "activation_pending")
        self.assertNotIn("raw", captured)

    def test_explicit_policy_cannot_claim_default_provenance(self) -> None:
        """Reserve the automatic-refresh marker for internal selection."""
        world = World(self)
        ctx = world.ctx(fresh=True)
        path = world.home / "policy.json"
        selected, _ = policy(
            ctx.data,
            world.home / "codex",
            "owner",
            1.0,
            repository_default=True,
        )
        path.write_text(canonical_json(selected))
        ctx.review_policy = path
        module = importlib.import_module("install.review_policy")
        with self.assertRaisesRegex(StepFailedError, "review_policy_invalid"):
            module.capture(ctx, world.sha)

    @unittest.skipIf(
        sys.platform == "win32",
        "Default Codex capability files require POSIX private-file checks.",
    )
    def test_unconfigured_upgrade_installs_the_current_default(self) -> None:
        """Apply the bundled policy to a store that has no policy yet."""
        world = World(self)
        ctx = world.ctx(fresh=True)
        installer.install(ctx, world.repo, world.sha)
        executable = world.home / "codex"
        executable.write_text("signed Codex fixture")
        executable.chmod(0o700)
        later = dataclasses.replace(
            ctx, fresh=False, upgrade=True, default_home=True, ts="later"
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("shutil.which", return_value=str(executable)),
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(executable, "1" * 64),
            ),
        ):
            result = installer.install(later, world.repo, world.sha)
        self.assertEqual(result["policy_status"], "active")

    def test_disabled_custom_policy_survives_default_upgrade(self) -> None:
        """Keep custom content and disabled admission unchanged."""
        world = World(self)
        ctx = world.ctx(fresh=True)
        path = world.home / "policy.json"
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        path.write_text(json.dumps(data))
        ctx.review_policy = path
        installer.install(ctx, world.repo, world.sha)
        with (
            store.writer_lock(ctx.data, wait_s=0),
            closing(store.connect_rw(store.db_path(ctx.data))) as conn,
        ):
            conn.execute("UPDATE review_control SET admission=0")
        before = store.connect_ro(store.db_path(ctx.data))
        with closing(before) as conn:
            expected = tuple(
                conn.execute(
                    "SELECT admission,policy_json,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )
        later = dataclasses.replace(
            ctx, fresh=False, upgrade=True, default_home=True, ts="later"
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
        ):
            installer.install(later, world.repo, world.sha)
        with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
            actual = tuple(
                conn.execute(
                    "SELECT admission,policy_json,policy_digest,activated_at "
                    "FROM review_control"
                ).fetchone()
            )
        self.assertEqual(actual, expected)

    @unittest.skipIf(
        sys.platform == "win32",
        "Default Codex capability files require POSIX private-file checks.",
    )
    def test_unused_default_refreshes_to_newer_profile(self) -> None:
        """Refresh an unused enabled default profile."""
        world = World(self)
        ctx = world.ctx(fresh=True, default_home=True)
        executable = world.home / "codex"
        executable.write_text("signed Codex fixture")
        executable.chmod(0o700)
        module = importlib.import_module("tools.prepare_codex_policy")
        profile = json.loads(module.DEFAULT_POLICY_FILE.read_text())
        profile["default_revision"] = 1
        path = world.home / "default-policy.json"
        path.write_text(json.dumps(profile))
        with (
            mock.patch.object(module, "DEFAULT_POLICY_FILE", path),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("shutil.which", return_value=str(executable)),
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(executable, "1" * 64),
            ),
        ):
            installer.install(ctx, world.repo, world.sha)
            profile["approved_at"] = "2026-10-24T00:01:00Z"
            profile["default_revision"] = 2
            path.write_text(json.dumps(profile))
            later = dataclasses.replace(
                ctx, fresh=False, upgrade=True, ts="later"
            )
            installer.install(later, world.repo, world.sha)
        with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
            row = conn.execute(
                "SELECT admission,policy_json FROM review_control"
            ).fetchone()
        selected = ReviewPolicy.model_validate_json(row["policy_json"])
        self.assertEqual(row["admission"], 1)
        self.assertEqual(
            selected.policy_id, "muninn-codex-ongoing-production-v2"
        )
