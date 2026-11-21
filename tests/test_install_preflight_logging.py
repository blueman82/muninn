"""Real installer entrypoint refusals must precede log or state writes."""

from __future__ import annotations

import contextlib
import functools
import io
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import installer as co
from tests.installer_support import World


class PreflightLoggingTests(unittest.TestCase):
    """Exercise main and the real preflight with isolated external effects."""

    def setUp(self) -> None:
        launcher = mock.patch(
            "install.provider_apps.shutil.which",
            return_value="/installed/claude-desktop",
        )
        launcher.start()
        self.addCleanup(launcher.stop)

    def test_refused_native_preflight_writes_nothing(self) -> None:
        world = World(self)
        world.home.joinpath(".claude/settings.json").chmod(0o666)
        before = {
            str(p.relative_to(world.home)): p.read_bytes()
            for p in world.home.rglob("*")
            if p.is_file()
        }
        names = {str(p.relative_to(world.home)) for p in world.home.rglob("*")}

        def run(
            argv: Sequence[str | Path],
            env: Mapping[str, str] | None = None,
            input: bytes | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            return world.fake.run(argv, env, input)

        output = io.StringIO()
        previous = os.umask(0o077)
        try:
            with (
                mock.patch.object(Path, "home", return_value=world.home),
                mock.patch.object(co, "run_real", run),
                mock.patch.object(
                    co,
                    "install_log",
                    functools.partial(co.install_log, run=run),
                ),
                contextlib.redirect_stdout(output),
            ):
                code = co.main(
                    [
                        "--repo",
                        str(world.repo),
                        "--sha",
                        world.sha,
                        "--fresh",
                        "--home",
                        str(world.home),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("unsafe", output.getvalue())
            self.assert_unchanged(world, names, before)
        finally:
            os.umask(previous)

    def assert_unchanged(
        self, world: World, names: set[str], before: dict[str, bytes]
    ) -> None:
        """Compare synthetic directory names and bytes after refusal."""
        self.assertEqual(
            names,
            {str(p.relative_to(world.home)) for p in world.home.rglob("*")},
        )
        self.assertEqual(
            before,
            {
                str(p.relative_to(world.home)): p.read_bytes()
                for p in world.home.rglob("*")
                if p.is_file()
            },
        )
