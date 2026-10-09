"""Only the managed writer records the release it actually loaded."""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from muninn import cli_serve, ingest, obs, obs_linux_service
from muninn.cli_maint import heartbeat
from muninn.obs_service_commands import render_unit
from tests.cli_support import CliCase


class WriterIdentityTests(CliCase):
    """Manual ingest cannot relabel an existing writer's loaded release."""

    def test_manual_heartbeat_preserves_actual_writer_release(self) -> None:
        loaded = "a" * 40
        env = {**self.env, "MUNINN_INSTALL_SHA": loaded}
        writer = cli_serve._Poller(self.home, env, 60)
        with patch.object(cli_serve.signal, "signal"):
            writer.prepare()
        before = obs.read_status(self.home)
        self.assertEqual(before.get("writer_install_sha"), loaded)
        self.assertEqual(before.get("pid"), os.getpid())
        heartbeat(
            self.home,
            ingest.PassStats(),
            {**self.env, "MUNINN_INSTALL_SHA": "b" * 40},
        )
        after = obs.read_status(self.home)
        self.assertEqual(after.get("install_sha"), "b" * 40)
        self.assertEqual(after.get("writer_install_sha"), loaded)
        self.assertEqual(after.get("pid"), os.getpid())
        user = Path(self.env["HOME"])
        python = Path(sys.executable).resolve()
        current = user / ".local/lib/muninn/current"
        unit = user / ".config/systemd/user/muninn.service"
        variables = {
            "HOME": str(user),
            "MUNINN_HOME": str(self.home),
            "CODEX_HOME": str(user / ".codex"),
            "CLAUDE_CONFIG_DIR": str(user / ".claude"),
        }

        def run(argv: Sequence[object]) -> subprocess.CompletedProcess[bytes]:
            response = (
                "LoadState=loaded\n"
                f"MainPID={os.getpid()}\nActiveState=active\n"
                f"FragmentPath={unit}\nDropInPaths=\n"
            ).encode()
            return subprocess.CompletedProcess(
                [str(value) for value in argv], 0, response
            )

        with (
            patch.object(
                obs_linux_service,
                "selection",
                return_value=(current, python, "b" * 40),
            ),
            patch.object(
                obs_linux_service,
                "read_unit",
                return_value=render_unit(
                    python, current, self.home, variables
                ),
            ),
            patch.object(obs_linux_service, "process_identity") as process,
        ):
            self.assertEqual(
                obs_linux_service.inspect(self.home, variables, run),
                (False, "writer_release_mismatch"),
            )
        process.assert_not_called()
        writer._alive()
        self.assertEqual(
            obs.read_status(self.home).get("writer_install_sha"), loaded
        )

    def test_clean_prepare_stop_still_keeps_run_crash_guard(self) -> None:
        with (
            patch.object(
                cli_serve._Poller,
                "prepare",
                side_effect=cli_serve._ServeStopError,
            ),
            patch.object(
                cli_serve._Poller,
                "run",
                side_effect=OSError("synthetic-private-detail"),
            ),
        ):
            result = cli_serve.serve(
                Namespace(interval=60), self.env, self.home, {}
            )
        self.assertEqual(result, (1, None))
        self.assertEqual(
            obs.read_status(self.home).get("last_error"), "OSError"
        )
