"""Real installer entrypoint refusals must precede log or state writes."""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from install import installer as co
from install.context import Ctx
from tests.installer_support import World, done


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
        for platform in ("linux", "win32", "unsafe_config"):
            native = "linux" if platform == "unsafe_config" else platform
            with self.subTest(platform=platform):
                world = World(self)
                if platform == "unsafe_config":
                    settings = world.home / ".claude/settings.json"
                    if os.name == "nt":
                        subprocess.run(
                            [
                                "icacls",
                                str(settings),
                                "/grant",
                                "*S-1-1-0:(R)",
                            ],
                            check=True,
                            capture_output=True,
                        )
                    else:
                        settings.chmod(0o666)
                before = {
                    str(p.relative_to(world.home)): p.read_bytes()
                    for p in world.home.rglob("*")
                    if p.is_file()
                }
                names = {
                    str(p.relative_to(world.home))
                    for p in world.home.rglob("*")
                }

                def run(
                    argv: Sequence[str | Path],
                    env: Mapping[str, str] | None = None,
                    input: bytes | None = None,
                    *,
                    current: World = world,
                    case: str = platform,
                ) -> subprocess.CompletedProcess[bytes]:
                    if str(argv[0]) == "systemctl":
                        if case == "unsafe_config":
                            return done(
                                b"LoadState=not-found\nMainPID=0\n"
                                b"ActiveState=inactive\nFragmentPath=\n"
                                b"DropInPaths=\n"
                            )
                        return done(rc=1)
                    if str(argv[0]).endswith("powershell.exe"):
                        return done(
                            json.dumps(
                                {
                                    "sid": "S-1-5-21-1-2-3-1001",
                                    "elevated": True,
                                }
                            ).encode()
                        )
                    return current.fake.run(argv, env, input)

                output = io.StringIO()
                previous = os.umask(0o077)
                try:
                    with (
                        mock.patch.object(
                            Path, "home", return_value=world.home
                        ),
                        mock.patch.object(
                            co,
                            "Ctx",
                            functools.partial(Ctx, platform=native),
                        ),
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
                    expected = {
                        "linux": "systemctl --user exited 1",
                        "win32": "installer as an ordinary Windows user",
                        "unsafe_config": "unsafe",
                    }[platform]
                    self.assertIn(expected, output.getvalue())
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
