"""Exercise learning scope choices through the real CLI."""

from __future__ import annotations

from tests.test_cli_learning import CliLearningCase


class LearningSetupCliTests(CliLearningCase):
    """Keep preview read-only and show the confirmation requirement."""

    def test_scope_preview_and_future_only_selection(self) -> None:
        self.save(self.result([]))
        code, preview = self.call("learn", "setup", "--mode", "all")
        self.assertEqual(code, 0)
        self.assertIn("confirmation", preview)
        self.assertEqual(
            preview["scope"],
            "All indexed projects; global records are excluded.",
        )
        code, selected = self.call("learn", "setup", "--mode", "now")
        self.assertEqual(code, 0)
        self.assertEqual(selected["mode"], "now")
