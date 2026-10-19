"""Disposable process-crash probes for retained installer operations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from install.context import Ctx
from install.lifecycle import linux_unit
from tests.legacy_upgrade_support import LegacyCase, LinuxService

CRASH = """
import os, sys
from pathlib import Path
from collections.abc import Mapping, Sequence
from tests.legacy_upgrade_support import LinuxService, LegacyCase
from unittest import mock
from install import (
    installer, legacy_publication, legacy_recovery, cutover, record
)
from install.context import Ctx
from tests.installer_support import Fake
from tests.legacy_upgrade_support import BOOT_B
home, repo, sha, cut = sys.argv[1:]
fake = Fake(Path(home))
ctx = Ctx(Path(home), fake.run, "crash-resume", upgrade=True,
          platform="darwin", now=fake.now,
          sleep=fake.sleep, say=lambda _: None)
original_publish = legacy_publication.platform_rebuild.publish_replacement
original_configure = legacy_recovery.configure
original_expose = legacy_recovery.expose
original_start = cutover.start_new
original_save = record.save

def publish(*args):
    if cut == "before_publish": os._exit(91)
    original_publish(*args)
    if cut == "after_publish": os._exit(91)

def configure(*args):
    original_configure(*args)
    if cut == "after_configure": os._exit(91)

def expose(*args):
    original_expose(*args)
    if cut == "after_expose": os._exit(91)

def start(*args):
    original_start(*args)
    if cut == "after_start": os._exit(91)

def save(ctx, rec):
    if cut == "after_policy_commit" and rec.get("policy_status") == "active":
        os._exit(91)
    original_save(ctx, rec)

with (
    mock.patch.object(record, "save", save),
    mock.patch("muninn.review_boot_identity.observe", return_value=BOOT_B),
    mock.patch.object(legacy_publication.platform_rebuild,
                      "publish_replacement", publish),
    mock.patch.object(legacy_recovery, "configure", configure),
    mock.patch.object(legacy_recovery, "expose", expose),
    mock.patch.object(cutover, "start_new", start),
):
    installer.install(ctx, Path(repo), sha)
"""


def crash(
    ctx: Ctx, repo: Path, sha: str, cut: str
) -> subprocess.CompletedProcess[bytes]:
    """Exit a separate installer process while preserving its actual files."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            CRASH,
            str(ctx.home),
            str(repo),
            sha,
            cut,
        ],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        check=False,
        timeout=30,
    )


class LinuxConsumerService(LinuxService):
    """Extend the existing isolated unit fake through compatible startup."""

    def __init__(self, fixture: LegacyCase) -> None:
        """Retain fake process state inside the disposable HOME."""
        super().__init__(fixture)
        self.fake = fixture.world.fake
        self.active = False

    def __call__(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Report registration and fake PIDs without real services."""
        if list(map(str, argv)) == [str(self.ctx.muninn), "doctor"]:
            expected = linux_unit(
                self.ctx,
                self.ctx.lib.joinpath("python").readlink(),
                self.ctx.release,
            )
            valid = self.active and self.ctx.plist.read_text() == expected
            data = json.dumps(
                {"checks": [{"check": "managed_service", "ok": valid}]}
            ).encode()
            return subprocess.CompletedProcess(
                argv, 0 if valid else 1, data, b""
            )
        if str(argv[0]) == "systemctl":
            command = str(argv[2])
            if command == "stop":
                self.active = False
                self.fake.loaded = None
            if command == "start":
                self.active = True
                self.fake.loaded = str(self.ctx.lib / "current/bin/muninn")
                self.fake._heartbeat()
            if command == "show" and self.active:
                values = {
                    "LoadState": "loaded",
                    "MainPID": str(self.fake.pid),
                    "ActiveState": "active",
                    "FragmentPath": str(self.ctx.plist),
                    "DropInPaths": "",
                }
                data = "".join(
                    f"{key}={value}\n" for key, value in values.items()
                ).encode()
                return subprocess.CompletedProcess(argv, 0, data, b"")
        return super().__call__(argv, env, input)
