"""Check the old unknown-setup notice can no longer be shown."""

from __future__ import annotations

import unittest
from pathlib import Path

from muninn import cli_learn_explain, review_recovery
from muninn.review_recovery_codes import EXPLANATIONS

_OLD = (
    "could not verify the review setup",
    "specific cause was not recorded",
)
_ROOT = Path(__file__).resolve().parents[1]
_TREES = (
    "muninn",
    "docs",
    ".claude/skills",
    ".codex/skills",
    "integrations",
)
_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".sh", ".yaml", ".yml"}


def _has_old(text: str) -> bool:
    """Say whether text holds either retired sentence."""
    flat = " ".join(text.lower().split())
    return any(phrase in flat for phrase in _OLD)


class CauseWordingTests(unittest.TestCase):
    """The retired sentence is absent everywhere a user can see it."""

    def test_no_explanation_has_the_old_sentence(self) -> None:
        for code in EXPLANATIONS:
            for stage in ("launch", "not recorded"):
                detail = review_recovery.explain(code, stage)
                self.assertFalse(_has_old(detail["cause"]), code)
                self.assertFalse(_has_old(detail["action"]), code)

    def test_unknown_code_falls_back_to_the_honest_text(self) -> None:
        detail = review_recovery.explain("no_such_code", "launch")
        self.assertEqual(detail["code"], "config")
        self.assertFalse(_has_old(detail["cause"] + detail["action"]))
        self.assertIn("no safe cause code", detail["cause"])

    def test_cli_config_action_is_reworded(self) -> None:
        action = cli_learn_explain._action
        text = str(action("config", "request-1", "failed"))
        self.assertFalse(_has_old(text))
        self.assertIn("muninn doctor", text)

    def test_no_shipped_text_carries_the_old_sentence(self) -> None:
        seen = 0
        for tree in _TREES:
            for path in sorted((_ROOT / tree).rglob("*")):
                if path.is_file() and path.suffix in _SUFFIXES:
                    seen += 1
                    text = path.read_text(encoding="utf-8", errors="replace")
                    self.assertFalse(_has_old(text), str(path))
        self.assertGreater(seen, 20)


if __name__ == "__main__":
    unittest.main()
