"""The SessionStart hook reports an absent installed policy precisely."""

from __future__ import annotations

from unittest import mock

from muninn import hook
from tests.hook_support import HookCase, notice


class HookPolicyRequiredTests(HookCase):
    """Policy setup failures should stay distinct from unexpected errors."""

    def test_missing_policy_has_a_specific_notice(self) -> None:
        with mock.patch.object(
            hook,
            "installed_policy",
            side_effect=ValueError("review_policy_required"),
        ):
            self.assertEqual(
                self.start_hook(), notice("review_policy_required")
            )
