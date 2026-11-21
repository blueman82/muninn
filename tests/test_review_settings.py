"""Check the learner settings file and the policy built from it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from muninn import review_settings
from muninn.review_instructions import PROMPT_DIGEST


class SettingsTests(unittest.TestCase):
    """Load, save and reject settings."""

    def test_missing_file_gives_claude_sonnet_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got = review_settings.load(Path(tmp))
        self.assertEqual(
            (got.enabled, got.provider, got.model, got.effort),
            (False, "claude", "sonnet", "low"),
        )

    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            want = review_settings.Settings(
                enabled=True, provider="codex", model="m", effort="high"
            )
            review_settings.save(home, want)
            self.assertEqual(review_settings.load(home), want)

    def test_unknown_key_and_bad_json_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / review_settings.FILE_NAME
            for raw in ('{"extra": 1}', "nope", '{"provider": "x"}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    review_settings.load(Path(tmp))

    def test_policy_follows_settings(self) -> None:
        policy = review_settings.policy_for(
            review_settings.Settings(model="opus", effort="high")
        )
        runner = policy.runner
        self.assertEqual(
            (runner.runner, runner.model, runner.reasoning),
            ("claude_cli", "opus", "high"),
        )
        self.assertEqual(policy.prompt_digest, PROMPT_DIGEST)
        self.assertEqual(policy.hook_wait_seconds, 0)


if __name__ == "__main__":
    unittest.main()
