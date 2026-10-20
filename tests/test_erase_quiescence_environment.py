"""Keep disposable owner children bound to their native temporary home."""

from __future__ import annotations

import ntpath
import os
import unittest
from pathlib import Path
from unittest import mock

from muninn import store
from tests import test_erase_quiescence as fixtures


class OwnerEnvironmentTests(unittest.TestCase):
    """Verify the owner subprocess environment in disposable storage."""

    def test_owner_child_environment_resolves_windows_home(self) -> None:
        fixture = fixtures.EraseQuiescenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        child = mock.Mock(returncode=0)
        child.communicate.return_value = (b"", b"")

        def ready(path: Path) -> None:
            store.write_json_atomic(path, {})

        with (
            mock.patch.object(
                fixtures.subprocess, "Popen", return_value=child
            ) as run,
            mock.patch.object(fixtures, "wait_file", side_effect=ready),
        ):
            fixture.start_owner()
        with mock.patch.dict(
            os.environ, run.call_args.kwargs["env"], clear=True
        ):
            self.assertEqual(ntpath.expanduser("~"), str(fixture.tmp))
