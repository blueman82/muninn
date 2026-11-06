"""Share the temporary installer boundary for managed interface tests."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from install import codex_interface, installer
from install.record import Record
from tests.installer_support import git
from tests.test_codex_interface_preview import InterfacePreviewTests


class InterfaceApplyFixture(InterfacePreviewTests):
    """Use real installer steps with fake services and temporary HOME."""

    def managed_release(self) -> None:
        """Commit the actual data parser source in the temporary fake repo."""
        root = Path(__file__).resolve().parents[1]
        for name in (
            "review_runtime_models.py",
            "review_codex_policy.py",
            "review_models.py",
        ):
            relative = Path("muninn") / name
            (self.repo / relative).write_bytes((root / relative).read_bytes())
            git(self.repo, "add", str(relative))
        git(self.repo, "commit", "-qm", "fixture managed interface")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()

    def apply(self, plan: codex_interface.InterfacePlan) -> Record:
        """Run installer code against fake native boundaries and temp HOME."""
        self.ctx.dry_run = False
        self.ctx.codex_interface = plan.digest
        self.ctx.interface_plan = plan
        with mock.patch(
            "muninn.review_managed_files.matches", return_value=True
        ):
            return installer.install(self.ctx, self.repo, self.sha)
