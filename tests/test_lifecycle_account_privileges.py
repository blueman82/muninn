"""Account probe privilege diagnostics are finite and read only."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.lifecycle_account_native import outer_script
from tests.lifecycle_account_privileges import SOURCE


class CallerPrivilegeTests(unittest.TestCase):
    """Do not confuse a disabled held privilege with an absent privilege."""

    def test_native_query_is_read_only_and_limited_to_three_privileges(
        self,
    ) -> None:
        self.assertIn("GetTokenInformation(token,3", SOURCE)
        self.assertIn("LookupPrivilegeValueW", SOURCE)
        for name in (
            "SeIncreaseQuotaPrivilege",
            "SeAssignPrimaryTokenPrivilege",
            "SeImpersonatePrivilege",
        ):
            self.assertIn(name, SOURCE)
        self.assertIn("attributes&2", SOURCE)
        self.assertIn("Marshal.FreeHGlobal", SOURCE)
        self.assertNotIn("AdjustTokenPrivileges", SOURCE)
        self.assertNotIn("SetTokenInformation", SOURCE)

    def test_query_uses_exact_caller_and_precedes_account_creation(
        self,
    ) -> None:
        script = outer_script(Path("/synthetic"))
        query = script.index("MuninnCiPrivileges]::Read($caller.Token)")
        self.assertLess(query, script.index("New-LocalUser"))
        self.assertIn("$caller.Dispose()", script)
