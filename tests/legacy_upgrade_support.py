"""Exact historical release fixtures with isolated current installer inputs."""

from __future__ import annotations

import ast
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from unittest import mock

from install.constants import GIT_ENV
from install.provider_paths import render_pinned
from tests.installer_support import ROOT, World

BASELINE = "5c1a69b0edff81df80836d72a80971a91a8f9520"
BOOT_A = "darwin:11111111-1111-4111-8111-111111111111"
BOOT_B = "darwin:22222222-2222-4222-8222-222222222222"


@lru_cache
def baseline_archive() -> bytes:
    """Read the pinned baseline as data without importing old runtime code."""
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE],
        env=GIT_ENV,
    )


def schema_sql(source: str) -> str:
    """Compose literal baseline DDL without executing its Python module."""
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id != "SCHEMA_SQL"
    }
    return (
        values["SCOPE_SQL"]
        + values["_SOURCES_SQL"]
        + values["KNOWLEDGE_TABLE"].format(name="knowledge")
        + ";\n"
        + values["_FTS_SQL"]
        + ";\n".join(values["KNOWLEDGE_TRIGGERS"])
        + ";\n"
        + values["_CITATION_SQL"]
        + values["_LOG_SQL"]
        + values["_TOMBSTONE_SQL"]
    )


class LegacyCase(unittest.TestCase):
    """Preserve a real baseline installation beneath a disposable home."""

    def setUp(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires the exact POSIX legacy installation")
        self.world = World(self)
        self.ctx = self.world.ctx(upgrade=True)
        self.ctx.settings.unlink()
        self.ctx.config.unlink()
        self.ctx.lib.mkdir(parents=True, mode=0o700)
        self.old = self.ctx.lib / BASELINE
        self.old.mkdir(mode=0o700)
        with tarfile.open(fileobj=io.BytesIO(baseline_archive())) as archive:
            archive.extractall(self.old, filter="data")
        for sub in ("integrations", "launchd"):
            for path in (self.old / sub).rglob("*"):
                if path.is_file() and b"@HOME@" in path.read_bytes():
                    path.write_bytes(
                        render_pinned(
                            self.ctx,
                            path.relative_to(self.old).as_posix(),
                            path.read_bytes(),
                        )
                    )
        self.python = self.world.home.parent / "old-python"
        self.python.write_bytes(b"isolated executable identity fixture")
        self.python.chmod(0o700)
        (self.ctx.lib / "current").symlink_to(BASELINE)
        (self.ctx.lib / "python").symlink_to(self.python)
        self.ctx.muninn.symlink_to(self.ctx.lib / "current/bin/muninn")
        self.ctx.plist.parent.mkdir(parents=True)
        self.ctx.plist.write_bytes(
            (self.old / "launchd/com.muninn.plist").read_bytes()
        )
        self.ctx.plist.chmod(0o644)
        self.world.fake.loaded = str(self.ctx.lib / "current/bin/muninn")
        self.ctx.data.mkdir(parents=True, mode=0o700)
        database = self.ctx.data / "muninn.sqlite"
        with sqlite3.connect(database) as conn:
            conn.executescript(
                schema_sql((self.old / "muninn/store_schema.py").read_text())
            )
            conn.execute("PRAGMA user_version=3")
            conn.execute(
                "INSERT INTO scope VALUES(1,'global','Global','global')"
            )
        conn.close()
        database.chmod(0o600)
        for name, data in (
            ("tombstone.key", b"k" * 32),
            ("tombstones.jsonl", b""),
        ):
            path = self.ctx.data / name
            path.write_bytes(data)
            path.chmod(0o600)
        self.original = self.data_state()
        self.boot = mock.patch(
            "muninn.review_boot_identity.observe", return_value=BOOT_A
        )
        self.observed_boot = self.boot.start()
        self.addCleanup(self.boot.stop)

    def data_state(self) -> dict[str, tuple[int, int, bytes]]:
        """Bind source identities and bytes, excluding maintenance metadata."""
        return {
            name: (
                (path := self.ctx.data / name).stat().st_dev,
                path.stat().st_ino,
                path.read_bytes(),
            )
            for name in ("muninn.sqlite", "tombstone.key", "tombstones.jsonl")
        }

    def assert_source_retained(self) -> None:
        """Require no store mutation, candidate, archive or runtime witness."""
        self.assertEqual(self.data_state(), self.original)
        names = {p.name for p in self.ctx.data.iterdir()}
        self.assertFalse(any("rebuild" in n or "upgrade" in n for n in names))
        self.assertFalse(any("witness" in n or "baseline" in n for n in names))

    def summary(self) -> dict[str, object]:
        """Read the durable installer outcome from this fixture only."""
        return json.loads((self.ctx.lib / "install-record.json").read_bytes())

    def hook(self) -> subprocess.CompletedProcess[bytes]:
        """Execute the replacement stub without running the old runtime."""
        self.assertNotEqual(self.ctx.muninn.resolve(), self.old / "bin/muninn")
        return subprocess.run(
            [self.ctx.muninn, "hook", "session-start", "--provider", "codex"],
            input=b"{}",
            capture_output=True,
            env=dict(os.environ, HOME=str(self.ctx.home)),
            check=False,
        )


class LinuxService:
    """Model a registered but idle unit using only the disposable context."""

    def __init__(self, fixture: LegacyCase) -> None:
        """Retain the underlying Git/runtime fake and finite service state."""
        self.ctx = fixture.ctx
        self.run = self.ctx.run
        self.enabled = True
        self.registered = True
        self.calls: list[str] = []

    def __call__(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Emulate service management and forward isolated fixture work."""
        if str(argv[0]) != "systemctl":
            return self.run(argv, env, input)
        command = str(argv[2])
        self.calls.append(command)
        if command == "disable":
            self.enabled = False
        if command == "daemon-reload":
            self.registered = self.ctx.plist.exists()
        if command == "show":
            values = {
                "LoadState": "loaded" if self.registered else "not-found",
                "MainPID": "0",
                "ActiveState": "inactive",
                "FragmentPath": str(self.ctx.plist) if self.registered else "",
                "DropInPaths": "",
            }
            data = "".join(
                f"{key}={value}\n" for key, value in values.items()
            ).encode()
        else:
            data = b""
        return subprocess.CompletedProcess(argv, 0, data, b"")
