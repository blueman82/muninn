"""Check safe repository-default policy selection during installation."""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import installer
from install.context import StepFailedError
from muninn import store
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from tests.installer_provider_env import fixture_provider_env
from tests.installer_support import World, git


class DefaultPolicySafetyTests(unittest.TestCase):
    """Keep automatic policy selection read-only and runner-bound."""

    def commit_profile(
        self, world: World, profile: dict[str, object], message: str
    ) -> str:
        """Commit the profile selected by an installer test.

        Args:
            world: Temporary repository used by the installer.
            profile: Complete JSON profile to commit.
            message: Commit message for the fixture revision.

        Returns:
            Full SHA of the commit.
        """
        path = world.repo / "tools/default_codex_policy.json"
        path.write_text(json.dumps(profile))
        git(world.repo, "add", "tools/default_codex_policy.json")
        git(world.repo, "commit", "-qm", message)
        return git(world.repo, "rev-parse", "HEAD").decode().strip()

    def test_selection_does_not_create_a_writer_lock(self) -> None:
        """Leave an existing store unchanged during policy discovery."""
        world = World(self)
        ctx = world.ctx(fresh=True)
        installer.install(ctx, world.repo, world.sha)
        lock_path = ctx.data / "writer.lock"
        lock_path.unlink()
        with fixture_provider_env(world.home):
            later = dataclasses.replace(
                ctx, fresh=False, upgrade=True, default_home=True
            )
        module = importlib.import_module("install.review_policy_default")
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("shutil.which", return_value=None),
        ):
            self.assertIsNone(module.select(later, b"{}"))
        self.assertFalse(lock_path.exists())

    def test_invalid_profile_refuses_before_executable_checks(self) -> None:
        """Reject malformed or out-of-bounds profiles during preflight."""
        world = World(self)
        with fixture_provider_env(world.home):
            ctx = world.ctx(fresh=True, default_home=True)
        executable = world.home / "codex"
        executable.write_text("signed Codex fixture")
        executable.chmod(0o700)
        module = importlib.import_module("install.review_policy_default")
        policy_module = importlib.import_module("tools.prepare_codex_policy")
        bounded = json.loads(policy_module.DEFAULT_POLICY_FILE.read_text())
        bounded["limits"]["max_rounds"] = 3
        invalid_profiles = (b"{", json.dumps(bounded).encode())
        for raw in invalid_profiles:
            with (
                self.subTest(raw=raw[:20]),
                mock.patch("platform.system", return_value="Darwin"),
                mock.patch("platform.machine", return_value="arm64"),
                mock.patch("shutil.which", return_value=str(executable)),
                mock.patch(
                    "muninn.review_codex_launch.verify_codex_executable",
                    side_effect=AssertionError("profile checked too late"),
                ),
                self.assertRaisesRegex(
                    StepFailedError, "default_review_policy_invalid"
                ),
            ):
                module.select(ctx, raw)
        self.assertFalse(ctx.data.exists())

    @unittest.skipIf(
        sys.platform == "win32",
        "Default Codex capability files require POSIX private-file checks.",
    )
    def test_refresh_keeps_installed_runner_when_path_changes(self) -> None:
        """Refresh policy bounds without rebinding the installed runner."""
        world = World(self)
        with fixture_provider_env(world.home):
            ctx = world.ctx(fresh=True, default_home=True)
        original = world.home / "codex-original"
        newly_discovered = world.home / "codex-new"
        new_auth_home = world.home / "codex-new-account"
        new_auth_home.mkdir(mode=0o700)
        for executable in (original, newly_discovered):
            executable.write_text("signed Codex fixture")
            executable.chmod(0o700)
        module = importlib.import_module("tools.prepare_codex_policy")
        profile = json.loads(module.DEFAULT_POLICY_FILE.read_text())
        first_sha = self.commit_profile(world, profile, "select default")
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch("shutil.which", return_value=str(original)),
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(original, "1" * 64),
            ),
        ):
            installer.install(ctx, world.repo, first_sha)
        profile["approved_at"] = "2026-10-24T00:01:00Z"
        profile["limits"]["max_rounds"] = 1
        second_sha = self.commit_profile(
            world, profile, "reduce default policy rounds"
        )
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CONFIG_DIR": str(world.home / ".claude"),
                "CODEX_HOME": str(new_auth_home),
            },
        ):
            later = dataclasses.replace(
                ctx,
                fresh=False,
                upgrade=True,
                default_home=True,
                ts="later",
            )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch(
                "shutil.which", return_value=str(newly_discovered)
            ) as which,
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(original, "1" * 64),
            ) as verify,
        ):
            installer.install(later, world.repo, second_sha)
        which.assert_not_called()
        verify.assert_called_with(original)
        with closing(store.connect_ro(store.db_path(ctx.data))) as conn:
            row = conn.execute(
                "SELECT admission,policy_json FROM review_control"
            ).fetchone()
        selected = ReviewPolicy.model_validate_json(row["policy_json"])
        self.assertEqual(row["admission"], 1)
        self.assertEqual(selected.runner.executable, str(original))
        self.assertEqual(selected.max_rounds, 1)
        capability_path = (
            ctx.data
            / "review-capabilities"
            / "selected"
            / (digest(selected) + ".json")
        )
        capability = json.loads(capability_path.read_text())
        binding = json.loads(Path(capability["proof_path"]).read_text())
        self.assertEqual(binding["auth_home"], str(ctx.codex_home))


if __name__ == "__main__":
    unittest.main()
