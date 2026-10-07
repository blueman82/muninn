"""Shared fixtures for the scope tests.

Temp repos and temp dirs only; HOME is patched to a temp dir.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import store

NO_HASH = "0" * 40


class ScopeCase(unittest.TestCase):
    """Base case with a temp HOME, a temp store and git helpers."""

    def setUp(self) -> None:
        """Create the temp home, point HOME at it and pick a store path."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))  # /var -> /private/var
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.db = self.tmp / "db" / "muninn.sqlite"

    def rw(self) -> sqlite3.Connection:
        """Open the temp store for writing; closed on cleanup.

        Returns:
            The read-write connection.
        """
        conn = store.connect_rw(self.db, fullfsync=False)
        self.addCleanup(conn.close)
        return conn

    def ro(self) -> sqlite3.Connection:
        """Open the temp store read-only; closed on cleanup.

        Returns:
            The read-only connection.
        """
        conn = store.connect_ro(self.db)
        self.addCleanup(conn.close)
        return conn

    def git(self, *args: str, cwd: Path) -> str:
        """Run git in a hermetic environment.

        Args:
            *args: Arguments after the fixed identity options.
            cwd: Directory to run in.

        Returns:
            Stripped standard output.
        """
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        cmd = [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
        ]
        cmd += ["-c", "commit.gpgsign=false", *args]
        done = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def make_repo(self, path: Path, message: str = "init") -> str:
        """Create a repo with one empty commit.

        Args:
            path: Directory to create and initialise.
            message: Commit message.

        Returns:
            The HEAD commit hash.
        """
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", cwd=path)
        self.git("commit", "-q", "--allow-empty", "-m", message, cwd=path)
        return self.git("rev-parse", "HEAD", cwd=path)

    def row(
        self, conn: sqlite3.Connection, cwd: Path | str
    ) -> tuple[str, str, str]:
        """Return the cached (scope key, scope kind, method) for a cwd.

        Args:
            conn: Open store connection.
            cwd: Directory whose cache row is read.

        Returns:
            The cached triple; the test fails when nothing is cached.
        """
        found = conn.execute(
            "SELECT s.key, s.kind, sp.method FROM scope_path sp"
            " JOIN scope s ON s.id = sp.scope_id WHERE sp.cwd = ?",
            (str(cwd),),
        ).fetchone()
        assert found is not None
        return (found[0], found[1], found[2])
