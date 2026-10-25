"""Keep disposable owner children bound to their native temporary home."""

from __future__ import annotations

import ntpath
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from muninn import store
from tests import erase_pause_process as processes
from tests import test_erase_pause as fixtures
from tests.store_support import direct_python


class WindowsProcessError(OSError):
    """Carry simulated Win32 failure codes on every test host."""

    winerror: int


class OwnerEnvironmentTests(unittest.TestCase):
    """Verify the owner subprocess environment in disposable storage."""

    def test_owner_child_environment_resolves_windows_home(self) -> None:
        fixture = fixtures.ErasePauseTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        child = mock.Mock(returncode=0)
        child.communicate.return_value = (b"", b"")

        def ready(path: Path) -> None:
            store.write_json_atomic(path, {})

        with (
            mock.patch.object(
                fixtures,
                "direct_python",
                return_value=(
                    "synthetic-base-python",
                    {"__PYVENV_LAUNCHER__": "synthetic-venv-python"},
                ),
                create=True,
            ),
            mock.patch.object(
                fixtures.subprocess, "Popen", return_value=child
            ) as run,
            mock.patch.object(fixtures, "wait_file", side_effect=ready),
        ):
            fixture.start_owner()
        self.assertEqual(run.call_args.args[0][0], "synthetic-base-python")
        self.assertEqual(
            run.call_args.kwargs["env"]["__PYVENV_LAUNCHER__"],
            "synthetic-venv-python",
        )
        with mock.patch.dict(
            os.environ, run.call_args.kwargs["env"], clear=True
        ):
            self.assertEqual(ntpath.expanduser("~"), str(fixture.tmp))

    def test_process_probe_distinguishes_live_and_exited_child(self) -> None:
        executable, environment = direct_python()
        with subprocess.Popen(
            [executable, "-c", "import time; time.sleep(30)"],
            env=os.environ | environment,
        ) as child:
            try:
                self.assertFalse(processes.process_gone(child.pid))
            finally:
                child.kill()
                child.wait(timeout=5)
            self.assertTrue(processes.process_gone(child.pid))

    def test_windows_process_probe_checks_completion_and_closes_handle(
        self,
    ) -> None:
        native = mock.Mock(SYNCHRONIZE=0x100000, WAIT_OBJECT_0=0)
        native.OpenProcess.return_value = 42
        with (
            mock.patch.object(
                processes, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(processes, "_winapi", native, create=True),
            mock.patch.object(
                processes.os, "kill", side_effect=OSError("invalid signal")
            ) as signal,
        ):
            for status, gone in ((258, False), (0, True)):
                with self.subTest(status=status):
                    native.WaitForSingleObject.return_value = status
                    self.assertEqual(processes.process_gone(123), gone)
            signal.assert_not_called()
        native.OpenProcess.assert_has_calls(
            [mock.call(0x100000, False, 123)] * 2
        )
        native.CloseHandle.assert_has_calls([mock.call(42)] * 2)

    def test_windows_process_probe_refuses_unverified_errors(self) -> None:
        native = mock.Mock(SYNCHRONIZE=0x100000)
        missing, denied = WindowsProcessError("missing"), WindowsProcessError(
            "denied"
        )
        missing.winerror, denied.winerror = 87, 5
        with (
            mock.patch.object(
                processes, "sys", SimpleNamespace(platform="win32")
            ),
            mock.patch.object(processes, "_winapi", native, create=True),
        ):
            native.OpenProcess.side_effect = missing
            self.assertTrue(processes.process_gone(123))
            native.OpenProcess.side_effect = denied
            with self.assertRaises(WindowsProcessError) as caught:
                processes.process_gone(123)
            self.assertIs(caught.exception, denied)
        native.CloseHandle.assert_not_called()
