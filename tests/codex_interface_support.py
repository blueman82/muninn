"""Share the temporary installer boundary for managed interface tests."""

from __future__ import annotations

from unittest import mock

from install import codex_interface, installer
from install.record import Record
from tests.installer_support import git
from tests.test_codex_interface_preview import InterfacePreviewTests


class InterfaceApplyFixture(InterfacePreviewTests):
    """Use real installer steps with fake services and temporary HOME."""

    def managed_release(self) -> None:
        """Commit a text compatibility marker in the temporary fake repo."""
        marker = self.repo / "muninn/review_runtime_models.py"
        marker.write_text(
            '"""Support the fake managed runtime cont'
            'ract."""\n\nfrom __future__ import annotat'
            'ions\n\nCONTRACT = "codex_managed_interfac'
            'e_v1"\n'
        )
        git(self.repo, "add", "muninn/review_runtime_models.py")
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
