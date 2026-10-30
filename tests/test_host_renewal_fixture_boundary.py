"""Keep host-renewal executables inside the temporary fixture boundary."""

from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install.context import StepFailedError
from install.host_renewal_preview import preview
from tests.host_renewal_support import HostFixture, states


@unittest.skipIf(sys.platform == "win32", "macOS private-path contract")
class HostRenewalFixtureBoundaryTests(unittest.TestCase):
    """Exercise native file checks without changing the real interpreter."""

    def make_fixture(self, executable: str) -> HostFixture:
        """Construct an owned installation with one ambient interpreter."""
        fixture = HostFixture()
        self.addCleanup(fixture.doCleanups)
        with mock.patch.object(sys, "executable", executable):
            fixture.setUp()
        return fixture

    def assert_interpreter(self, executable: Path) -> None:
        """Require the pinned virtual environment and exact argument bytes."""
        arguments = ["two words", "'\";$(literal)", "", "*.py"]
        program = (
            "import json,sys;print(json.dumps({"
            "'executable':sys.executable,'prefix':sys.prefix,"
            "'base_prefix':sys.base_prefix,'argv':sys.argv[1:],"
            "'version':list(sys.version_info[:3])}))"
        )
        result = subprocess.run(
            [str(executable), "-I", "-B", "-c", program, *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "executable": sys.executable,
                "prefix": sys.prefix,
                "base_prefix": sys.base_prefix,
                "argv": arguments,
                "version": list(sys.version_info[:3]),
            },
        )

    def test_unsafe_ambient_interpreter_does_not_affect_preview(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        outside = Path(temporary).resolve()
        ambient = outside / "ambient python 'quoted'"
        ambient.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
        )
        for mode in (0o720, 0o702):
            with self.subTest(mode=mode):
                ambient.chmod(mode)
                self.assert_interpreter(ambient)
                original = states(outside)
                fixture = self.make_fixture(str(ambient))
                try:
                    before = states(fixture.home)
                    control = fixture.read_control()
                    try:
                        preview(fixture.ctx, fixture.repo, fixture.sha)
                    except StepFailedError as error:
                        self.fail(f"ambient metadata reached preview: {error}")
                    self.assertTrue(
                        fixture.executable.is_relative_to(fixture.home)
                    )
                    self.assert_interpreter(fixture.executable)
                    self.assertEqual(states(fixture.home), before)
                    self.assertEqual(fixture.read_control(), control)
                    self.assertEqual(states(outside), original)
                finally:
                    fixture.doCleanups()

    def test_owned_executable_preserves_pinned_interpreter_arguments(
        self,
    ) -> None:
        fixture = self.make_fixture(sys.executable)
        self.assertTrue(fixture.executable.is_relative_to(fixture.home))
        self.assertEqual(
            stat.S_IMODE(fixture.executable.stat().st_mode), 0o700
        )
        before = states(fixture.home)
        self.assert_interpreter(fixture.executable)
        self.assertEqual(states(fixture.home), before)

    def test_writable_owned_executable_refuses_without_changes(self) -> None:
        fixture = self.make_fixture(sys.executable)
        self.assertTrue(fixture.executable.is_relative_to(fixture.home))
        for mode in (0o720, 0o702):
            with self.subTest(mode=mode):
                fixture.executable.chmod(mode)
                before = states(fixture.home)
                control = fixture.read_control()
                with self.assertRaisesRegex(
                    StepFailedError, "legacy_file_unsafe"
                ):
                    preview(fixture.ctx, fixture.repo, fixture.sha)
                self.assertEqual(states(fixture.home), before)
                self.assertEqual(fixture.read_control(), control)
