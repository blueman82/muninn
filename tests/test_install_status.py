"""Compare local versions without suggesting a backward install."""

from __future__ import annotations

import os
import shlex
import shutil
import unittest

from tests import test_install_wrapper as support
from tests.installer_support import git


class InstallStatusTests(unittest.TestCase):
    """Compare temporary local histories without running an installer."""

    def setUp(self) -> None:
        self.fixture = support.WrapperTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.hook_repo()
        self.older = git(self.root, "rev-parse", "HEAD").decode().strip()
        git(self.root, "commit", "--allow-empty", "-qm", "newer")
        self.newer = git(self.root, "rev-parse", "HEAD").decode().strip()

    def status(self, installed: str) -> str:
        """Return read-only status for one installed revision."""
        self.fixture.installed(installed)
        before = sorted(self.fixture.home.rglob("*"))
        result = self.fixture.run_wrapper(
            "--status", script=self.root / "bin/muninn-install"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.argv(), "")
        self.assertEqual(sorted(self.fixture.home.rglob("*")), before)
        return result.stdout

    def test_older_install_can_upgrade_from_newer_checkout(self) -> None:
        output = self.status(self.older)
        self.assertIn("checkout is newer; upgrade available", output)
        self.assertIn("bin/muninn-install --check", output)

    def test_older_checkout_must_not_suggest_backward_install(self) -> None:
        git(self.root, "checkout", "--detach", self.older)
        output = self.status(self.newer)
        self.assertIn("installed version is newer", output)
        self.assertIn("update this checkout before installing", output)
        self.assertNotIn("upgrade available", output)
        self.assertNotIn("upgrade needed", output)

    def test_unknown_installed_commit_has_no_ordering_claim(self) -> None:
        output = self.status("a" * 40)
        self.assertIn("versions differ; ancestry unknown", output)
        self.assertIn("inspect versions before installing", output)
        self.assertNotIn("upgrade", output)

    def test_divergent_commits_have_no_upgrade_claim(self) -> None:
        git(self.root, "checkout", "--detach", self.older)
        git(self.root, "commit", "--allow-empty", "-qm", "divergent")
        output = self.status(self.newer)
        self.assertIn("versions differ; histories diverged", output)
        self.assertIn("inspect versions before installing", output)
        self.assertNotIn("upgrade", output)

    def test_absolute_release_link_uses_commit_directory_name(self) -> None:
        output = self.status(str(self.fixture.lib / self.newer))
        self.assertIn("up to date", output)

    def test_partial_install_is_not_reported_up_to_date(self) -> None:
        self.fixture.lib.mkdir(parents=True)
        (self.fixture.lib / "current").symlink_to(self.newer)
        result = self.fixture.run_wrapper(
            "--status", script=self.root / "bin/muninn-install"
        )
        self.assertIn("half-installed", result.stdout)
        self.assertNotIn("up to date", result.stdout)
        self.assertEqual(self.fixture.argv(), "")

    def test_shallow_history_does_not_prove_divergence(self) -> None:
        git(self.root, "checkout", "--detach", self.older)
        git(self.root, "commit", "--allow-empty", "-qm", "divergent")
        (self.root / ".git/shallow").write_text(self.newer + "\n")
        output = self.status(self.newer)
        self.assertIn("ancestry unknown", output)
        self.assertNotIn("histories diverged", output)

    def test_malformed_release_name_is_not_printed_as_version(self) -> None:
        output = self.status("private-release-name")
        self.assertIn("installed unknown", output)
        self.assertNotIn("private-release", output)

    def test_partial_clone_status_never_fetches_missing_history(self) -> None:
        log = self.root / "remote-calls"
        helper = self.root / "git-remote-muninn-status-probe"
        helper.write_text(
            "#!/bin/sh\n"
            f"echo called >> {shlex.quote(log.as_posix())}\nexit 1\n"
        )
        helper.chmod(0o755)
        git(
            self.root,
            "config",
            "remote.origin.url",
            "muninn-status-probe::fixture",
        )
        git(self.root, "config", "remote.origin.promisor", "true")
        git(
            self.root,
            "config",
            "remote.origin.partialclonefilter",
            "blob:none",
        )
        self.fixture.installed("a" * 40)
        real_git = shutil.which("git")
        assert real_git is not None
        legacy_git = self.root / "git"
        legacy_git.write_text(
            "#!/bin/sh\nunset GIT_NO_LAZY_FETCH\n"
            f'exec {shlex.quote(real_git)} "$@"\n'
        )
        legacy_git.chmod(0o755)
        result = self.fixture.run_wrapper(
            "--status",
            script=self.root / "bin/muninn-install",
            extra_env={
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "GIT_ALLOW_PROTOCOL": "muninn-status-probe",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ancestry unknown", result.stdout)
        self.assertFalse(log.exists(), "status invoked the remote transport")
