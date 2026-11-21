"""Show the learning-off notice at session start and stay quiet when on."""

from __future__ import annotations

from muninn import hook, review_settings
from tests.ingest_support import TID
from tests.test_cli_learning import CliLearningCase


class LearningStartupActionTests(CliLearningCase):
    """Read the real SessionStart output for the mapped project."""

    def startup(self) -> dict[str, object]:
        """Read startup output from the mapped project and isolated store."""
        return hook.session_start(
            {"session_id": TID, "cwd": str(self.cwd)},
            "codex",
            {"MUNINN_HOME": str(self.home)},
        )

    def test_learning_off_is_visible_and_on_is_quiet(self) -> None:
        review_settings.save(self.home, review_settings.Settings())
        self.assertIn("learning is off", str(self.startup()))
        review_settings.save(self.home, review_settings.Settings(enabled=True))
        self.assertNotIn("Muninn learning", str(self.startup()))
