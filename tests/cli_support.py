"""Shared fixtures for the muninn CLI test modules."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli
from tests.test_classify import codex_meta, reply, user_msg
from tests.test_ingest import TID, IngestCase, rollout

CANARY = "CANARY-CLI-" + "k4" * 9

ALWAYS = {"notice", "index_age_s", "poller", "logged"}


class CliCase(IngestCase):
    """Runs ``cli.main`` in-process against synthetic provider trees."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        roots = {k: str(v) for k, v in self.roots.items()}
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("MUNINN_", "CLAUDE_CODE_SESSION", "CODEX_"))
        }
        self.env = keep | {
            "MUNINN_HOME": str(self.home),
            "MUNINN_ROOTS": json.dumps(roots),
            "HOME": str(self.tmp / "userhome"),
        }

    def session(self, tid: str = TID, *texts: str) -> Path:
        """Write a codex rollout whose turns alternate user and reply.

        Args:
            tid: Thread id of the rollout.
            *texts: Message texts, starting with a user prompt.

        Returns:
            Path of the rollout file.
        """
        meta = codex_meta("user", tid, cwd=str(self.repo))
        records = [meta] + [
            (user_msg if n % 2 == 0 else reply)(n + 1, text)
            for n, text in enumerate(texts)
        ]
        return self.write(rollout(tid), records)

    def muninn(
        self, *argv: str, env: dict[str, str] | None = None
    ) -> tuple[int, Any, str]:
        """Run one ``muninn`` call in-process from inside the test repo.

        Args:
            *argv: Command line after ``muninn``.
            env: Variables layered over the isolated test environment.

        Returns:
            Exit code, parsed JSON (or raw text when stdout is not JSON)
            and stderr.
        """
        out, err, cwd = io.StringIO(), io.StringIO(), Path.cwd()
        patch = mock.patch.dict(os.environ, self.env | (env or {}), clear=True)
        with (
            patch,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            os.chdir(self.repo)
            try:
                code = cli.main(list(argv))
            finally:
                os.chdir(cwd)
        text = out.getvalue()
        try:
            return code, json.loads(text), err.getvalue()
        except ValueError:
            return code, text, err.getvalue()


def fake_run(
    pid: int | None = 4242, cmd: str | None = None, ps: str = ""
) -> Callable[[Sequence[object]], subprocess.CompletedProcess[bytes]]:
    """Build a launchctl/ps stand-in so tests never see the real launchd.

    Args:
        pid: Pid ``launchctl print`` reports; ``None`` means job not found.
        cmd: Command line ``ps -p`` reports for that pid.
        ps: Output of any other command.

    Returns:
        A replacement for ``obs.run``.
    """

    def run(
        argv: Sequence[object],
    ) -> subprocess.CompletedProcess[bytes]:
        args = [str(a) for a in argv]
        if args[:2] == ["launchctl", "print"]:
            if pid is None:
                return subprocess.CompletedProcess(args, 113, b"", b"")
            return subprocess.CompletedProcess(
                args, 0, f"\tstate = running\n\tpid = {pid}\n".encode(), b""
            )
        if args[:2] == ["ps", "-ww"] and "-p" in args:
            return subprocess.CompletedProcess(
                args, 0, (cmd or "").encode(), b""
            )
        return subprocess.CompletedProcess(args, 0, ps.encode(), b"")

    return run
