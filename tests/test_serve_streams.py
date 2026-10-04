"""The poller's stdout and stderr never reach ``poller.log`` raw."""

from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import IO
from unittest import mock

from muninn import cli_serve
from tests.cli_support import CliCase

ROOT = Path(__file__).resolve().parent.parent
CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from muninn import cli_serve\n"
    "cli_serve._quiet_streams(Path(sys.argv[1]))\n"
    "print('SECRET-OUT')\n"
    "print('SECRET-ERR', file=sys.stderr)\n"
)


class QuietStreamsTests(CliCase):
    """Streams that are the log are silenced; others are left alone."""

    def run_child(
        self, out: int | IO[bytes], err: int | IO[bytes]
    ) -> subprocess.CompletedProcess[str]:
        """Run the child with the given stdout and stderr targets.

        Args:
            out: Where the child's stdout goes.
            err: Where the child's stderr goes.

        Returns:
            The finished process.
        """
        return subprocess.run(
            [sys.executable, "-c", CHILD, str(self.home)],
            stdout=out, stderr=err, text=True, check=True,
            cwd=str(self.home.parent), env={"PYTHONPATH": str(ROOT)},
        )  # fmt: skip

    def test_streams_that_are_the_log_go_to_devnull(self) -> None:
        log = self.home / "poller.log"
        log.write_text("")
        with log.open("ab") as fh:
            self.run_child(fh, fh)
        self.assertEqual(log.read_text(), "")

    def test_other_streams_keep_their_output(self) -> None:
        (self.home / "poller.log").write_text("")
        done = self.run_child(subprocess.PIPE, subprocess.PIPE)
        self.assertIn("SECRET-OUT", done.stdout)
        self.assertIn("SECRET-ERR", done.stderr)


class CrashTests(CliCase):
    """An exception outside a pass is logged as a class name only."""

    def test_a_crash_logs_the_class_and_exits_nonzero(self) -> None:
        with mock.patch.object(
            cli_serve._Poller, "run", side_effect=RuntimeError("SECRET-TEXT")
        ):
            result = cli_serve.serve(
                Namespace(interval=60.0), self.env, self.home, {}
            )
        self.assertEqual(result, (1, None))
        text = (self.home / "poller.log").read_text()
        lines = [json.loads(x) for x in text.splitlines()]
        crash = [x for x in lines if x.get("event") == "crash"]
        self.assertEqual(crash[0]["exc"], "RuntimeError")
        self.assertNotIn("SECRET-TEXT", text)
