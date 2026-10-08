"""Real git effects in installer fakes remain inside one owned fixture."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from install import rollback, steps_release
from install.context import StepFailedError
from install.provider_paths import render_pinned
from tests import installer_fakes
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


class FixtureLinkModelTests(unittest.TestCase):
    """The Windows fake models link outcomes without claiming atomicity."""

    def test_both_aliases_model_upgrade_and_rollback_only_in_owned_home(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            with patch(
                "tests.installer_fakes.sys",
                SimpleNamespace(platform="win32"),
            ):
                installer_fakes.register(self, home)
            link = home / ".local/lib/muninn/current"
            for name in ("old", "new"):
                target = link.parent / name
                target.mkdir(parents=True, exist_ok=True)
                (target / "marker").write_text(name)
            steps_release.relink(link, "old", "1")
            steps_release.relink(link, "new", "2")
            self.assertEqual(link.readlink(), Path("new"))
            self.assertEqual((link / "marker").read_text(), "new")
            rollback.relink(link, "old", "3")
            self.assertEqual(link.readlink(), Path("old"))
            self.assertEqual((link / "marker").read_text(), "old")
            self.assertEqual((link.parent / "new/marker").read_text(), "new")
            self.assertEqual(list(link.parent.glob(".current.*")), [])

    def test_nonlink_outside_similar_prefix_and_parent_escape_refuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "owned"
            home.mkdir()
            link = home / ".local/lib/muninn/current"
            link.parent.mkdir(parents=True)
            link.write_text("untouched")
            outside = root / "owned-other/.local/lib/muninn/current"
            outside.parent.mkdir(parents=True)
            outside.write_text("foreign")
            for candidate in (link, outside, home / ".local/lib/muninn/other"):
                with (
                    self.subTest(candidate=candidate),
                    self.assertRaises(ValueError),
                ):
                    installer_fakes.relink(home, candidate, "new", "1")
            self.assertEqual(link.read_text(), "untouched")
            self.assertEqual(outside.read_text(), "foreign")
            link.unlink()
            link.parent.rmdir()
            link.parent.symlink_to(outside.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                installer_fakes.relink(home, link, "new", "2")
            self.assertEqual(outside.read_text(), "foreign")
            self.assertEqual(list(outside.parent.glob(".current.*")), [])

    def test_existing_temp_is_retained_and_posix_aliases_are_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            original = steps_release.relink, rollback.relink
            with patch(
                "tests.installer_fakes.sys", SimpleNamespace(platform="darwin")
            ):
                installer_fakes.register(self, home)
            self.assertEqual((steps_release.relink, rollback.relink), original)
            link = home / ".local/lib/muninn/current"
            link.parent.mkdir(parents=True)
            pending = link.with_name(".current.1")
            pending.write_text("unknown")
            with self.assertRaises(FileExistsError):
                installer_fakes.relink(home, link, "new", "1")
            self.assertEqual(pending.read_text(), "unknown")
            self.assertFalse(link.exists())
