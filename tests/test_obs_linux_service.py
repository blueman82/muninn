"""Linux service identity is bound to exact owned code and unit settings."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from install import lifecycle, snapshot
from install.context import Ctx, run_real
from muninn import obs_linux_service, obs_service_commands


class LinuxServiceTests(unittest.TestCase):
    """Exercise finite unit and actual process identity contracts."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.ctx = Ctx(self.home, run_real, "linux", platform="linux")
        self.python = Path(sys.executable).resolve()
        self.release = self.ctx.release
        self.env = {
            "HOME": str(self.home),
            "MUNINN_HOME": str(self.ctx.data),
            "CODEX_HOME": str(self.ctx.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.ctx.settings.parent),
        }
        self.unit = lifecycle.linux_unit(self.ctx, self.python, self.release)

    def test_owned_definition_refuses_critical_changes(self) -> None:
        for old, new in (
            ("SendSIGKILL=no", "SendSIGKILL=yes"),
            ("TimeoutStopSec=infinity", "TimeoutStopSec=1"),
            ("Restart=on-failure", "Restart=always"),
            (str(self.python), "/foreign/python"),
            ("UMask=0077", "UMask=0022"),
            ("[Service]\nType=simple", "Type=simple\n[Service]"),
        ):
            with self.subTest(old=old):
                self.assertFalse(
                    obs_linux_service.unit_matches(
                        self.unit.replace(old, new),
                        self.python,
                        self.release,
                        self.ctx.data,
                        self.env,
                    )
                )
        self.assertTrue(
            obs_linux_service.unit_matches(
                self.unit, self.python, self.release, self.ctx.data, self.env
            )
        )
        self.assertFalse(
            obs_linux_service.unit_matches(
                self.unit + "ExecStart=/foreign\n",
                self.python,
                self.release,
                self.ctx.data,
                self.env,
            )
        )

    def test_bootstrap_names_loaded_root_before_import(self) -> None:
        root = self.home / ("a" * 40)
        data = self.home / "data"
        root.mkdir(mode=0o700)
        data.mkdir(mode=0o700)
        package = root / "muninn"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text(
            "import os\n"
            "def main(args):\n"
            " print(os.environ['MUNINN_INSTALL_SHA'])\n"
            " return 0\n"
        )
        if sys.platform == "win32":
            self.assertIn("MUNINN_INSTALL_SHA", obs_service_commands.BOOTSTRAP)
            return
        result = subprocess.run(
            [str(self.python), *obs_service_commands.arguments(root, data)],
            capture_output=True,
            env={**os.environ, "MUNINN_INSTALL_SHA": "stale"},
            check=True,
        )
        self.assertEqual(result.stdout.strip(), b"a" * 40)

    def test_rollback_renders_from_restored_interpreter(self) -> None:
        with (
            patch.object(obs_linux_service, "selection") as selected,
            patch.object(lifecycle, "start") as start,
            patch.object(snapshot, "wait"),
        ):
            selected.return_value = (self.release, self.python, "a" * 40)
            snapshot.start_again(self.ctx)
        start.assert_called_once_with(self.ctx, self.python, self.release)

    def test_inspection_binds_unit_process_and_loaded_release(self) -> None:
        pid = 34567
        response = (
            f"MainPID={pid}\nActiveState=active\n"
            f"FragmentPath={self.ctx.plist}\nDropInPaths=\n"
        ).encode()
        argv = [
            str(self.python),
            *obs_service_commands.arguments(
                self.release, self.ctx.data, "serve", "--interval", "60"
            ),
        ]
        identity = ("123456", self.python, argv)
        status = {
            "pid": pid,
            "install_sha": "b" * 40,
            "writer_install_sha": "a" * 40,
        }
        with (
            patch.object(
                obs_linux_service,
                "selection",
                return_value=(self.release, self.python, "a" * 40),
            ),
            patch.object(
                obs_linux_service, "read_unit", return_value=self.unit
            ),
            patch.object(
                obs_linux_service, "read_status", return_value=status
            ),
            patch.object(
                obs_linux_service, "process_identity", return_value=identity
            ) as process,
        ):

            def run(
                command: Sequence[object],
            ) -> subprocess.CompletedProcess[bytes]:
                return subprocess.CompletedProcess(
                    [str(value) for value in command], 0, response
                )

            self.assertEqual(
                obs_linux_service.inspect(self.ctx.data, self.env, run),
                (True, pid),
            )
            self.assertEqual(process.call_count, 2)
            for field, value, code in (
                ("writer_install_sha", "b" * 40, "writer_release_mismatch"),
                ("pid", pid + 1, "writer_identity_mismatch"),
            ):
                with self.subTest(field=field):
                    original = status[field]
                    status[field] = value
                    self.assertEqual(
                        obs_linux_service.inspect(
                            self.ctx.data, self.env, run
                        ),
                        (False, code),
                    )
                    status[field] = original
            process.return_value = (
                "123456",
                self.python,
                [*argv, "--foreign"],
            )
            self.assertEqual(
                obs_linux_service.inspect(self.ctx.data, self.env, run),
                (False, "writer_command_mismatch"),
            )
            process.return_value = identity
            response = response.replace(
                b"DropInPaths=\n", b"DropInPaths=/foreign.conf\n"
            )
            self.assertEqual(
                obs_linux_service.inspect(self.ctx.data, self.env, run),
                (False, "unit_registration_mismatch"),
            )

    def test_proc_reads_refuse_oversized_and_malformed_identity(self) -> None:
        source = self.home / "stat"
        source.write_bytes(b"x" * 8193)
        with self.assertRaises(ValueError):
            obs_linux_service.proc_read(source, 8192)
        source.write_text("malformed")
        with self.assertRaises(ValueError):
            obs_linux_service.start_identity(self.home)
