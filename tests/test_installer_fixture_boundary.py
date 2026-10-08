"""Real git effects in installer fakes remain inside one owned fixture."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from install import steps_release
from install.context import StepFailedError
from install.provider_paths import render_pinned
from tests.installer_support import ROOT, Fake, World, git


class FakeGitBoundaryTests(unittest.TestCase):
    """Normalize native path spellings without broadening the fixture root."""

    def test_outside_and_similar_prefix_repositories_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            owned = parent / "owned"
            home = owned / "home"
            home.mkdir(parents=True)
            outside = parent / "owned-other" / "repo"
            outside.mkdir(parents=True)
            fake = Fake(home)
            with (
                patch("tests.installer_support.subprocess.run") as run,
                self.assertRaises(AssertionError),
            ):
                fake._git(["-C", str(outside), "rev-parse", "HEAD"], {}, None)
            run.assert_not_called()

    def test_owned_absolute_repo_ignores_global_temp_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            home, repo = parent / "home", parent / "repo"
            home.mkdir()
            repo.mkdir()
            fake = Fake(home)
            with (
                patch(
                    "tests.installer_support.tempfile.gettempdir",
                    return_value="/unrelated/temp",
                ),
                patch(
                    "tests.installer_support.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["git"], 0, b"sha"
                    ),
                ) as run,
            ):
                result = fake._git(
                    ["-C", str(repo), "rev-parse", "HEAD"], {}, None
                )
            self.assertEqual(result.stdout, b"sha")
            run.assert_called_once()


class FixtureLauncherModeTests(unittest.TestCase):
    """Git records launcher executability independently of host file modes."""

    def test_fixture_records_executable_launcher_from_nonexecuting_copy(
        self,
    ) -> None:
        world = World(self)
        launcher = world.repo / "bin/muninn"
        launcher.chmod(0o600)
        git(world.repo, "update-index", "--chmod=-x", "bin/muninn")
        git(world.repo, "commit", "-qm", "nonexecuting source")
        with patch("tests.installer_support.ROOT", world.repo):
            copied = World(self)
        mode = git(copied.repo, "ls-tree", "HEAD", "bin/muninn").split()[0]
        self.assertEqual(mode, b"100755")
        self.assertEqual(git(copied.repo, "status", "--porcelain"), b"")


class FixturePlistPathTests(unittest.TestCase):
    """The fake Darwin plist follows its real host filesystem spelling."""

    def test_windows_spelling_keeps_placeholder_source_and_is_idempotent(
        self,
    ) -> None:
        relative = "launchd/com.muninn.plist"
        original = (ROOT / relative).read_bytes()

        def native_path(value: str) -> Path | PureWindowsPath:
            return PureWindowsPath(value) if value == "@HOME@" else Path(value)

        with patch("tests.installer_support.Path", side_effect=native_path):
            world = World(self)
            first = (world.repo / relative).read_bytes()
            with patch("tests.installer_support.ROOT", world.repo):
                copied = World(self)
        expected = (
            f"{PureWindowsPath('@HOME@') / '.local/lib/muninn'}"
            "/current/bin/muninn"
        )
        self.assertEqual(
            plistlib.loads(first)["ProgramArguments"][0], expected
        )
        self.assertIn(b"@HOME@", first)
        self.assertEqual((copied.repo / relative).read_bytes(), first)
        self.assertEqual((ROOT / relative).read_bytes(), original)
        self.assertEqual(git(world.repo, "status", "--porcelain"), b"")

    def test_real_checker_keeps_exact_target_and_executable_refusals(
        self,
    ) -> None:
        world = World(self)
        ctx = world.ctx()
        program = ctx.lib / "current/bin/muninn"
        program.parent.mkdir(parents=True)
        shutil.copy2(world.repo / "bin/muninn", program)
        program.chmod(0o755)
        relative = "launchd/com.muninn.plist"
        data = render_pinned(
            ctx, relative, (world.repo / relative).read_bytes()
        )
        steps_release._check_plist(ctx, data)
        changed = plistlib.loads(data)
        changed["ProgramArguments"][0] += ".foreign"
        with self.assertRaises(StepFailedError):
            steps_release._check_plist(ctx, plistlib.dumps(changed))
        program.unlink()
        with self.assertRaises(StepFailedError):
            steps_release._check_plist(ctx, data)
