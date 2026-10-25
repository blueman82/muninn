"""SessionStart does not depend on the installed learning policy."""

from __future__ import annotations

from unittest import mock

from muninn import cli_learn
from tests.hook_support import HookCase, notice


class HookPolicyRequiredTests(HookCase):
    """Read-only SessionStart does not require a learning policy."""

    def test_missing_policy_does_not_block_session_start(self) -> None:
        knowledge_id = self.add(content="Keep the zebra cache enabled")
        with mock.patch.object(
            cli_learn,
            "installed_policy",
            side_effect=ValueError("review_policy_required"),
        ) as installed:
            self.assertNotEqual(
                self.start_hook(), notice("review_policy_required")
            )
            self.assertIn(knowledge_id, str(self.start_hook()))
            installed.assert_not_called()
