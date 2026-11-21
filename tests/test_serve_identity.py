"""Only the managed writer records the release it actually loaded."""

from __future__ import annotations

import os
from argparse import Namespace
from unittest.mock import patch

from muninn import cli_serve, ingest, obs
from muninn.cli_maint import heartbeat
from tests.cli_support import CliCase
from tests.home_support import initialize


class WriterIdentityTests(CliCase):
    """Manual ingest cannot relabel an existing writer's loaded release."""

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)

    def test_manual_heartbeat_preserves_actual_writer_release(self) -> None:
        loaded = "a" * 40
        env = {**self.env, "MUNINN_INSTALL_SHA": loaded}
        writer = cli_serve._Poller(self.home, env, 60)
        self.addCleanup(writer.close)
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
