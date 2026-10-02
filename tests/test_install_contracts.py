"""Installer log, command line and the Codex contract it pins."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from install import installer as co
from install import rollback as rb
from tests.installer_support import REAL_CODEX, ROOT


class InstallLogTest(unittest.TestCase):
    """The install log keeps status and stderr and drops stdout."""

    def test_log_has_status_and_stderr_but_never_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lib/install.log"

            def run(
                argv: Sequence[str | Path],
                env: Mapping[str, str] | None = None,
                input: bytes | None = None,
            ) -> subprocess.CompletedProcess[bytes]:
                return subprocess.CompletedProcess(
                    argv, 3, b"TRANSCRIPT-TEXT", b"boom"
                )

            with contextlib.redirect_stdout(io.StringIO()):
                say, logged = co.install_log(path, run)
                say("hello")
                logged(["launchctl", "bootstrap", "x"])
            text = path.read_text()
            self.assertIn("hello", text)
            self.assertIn("run launchctl bootstrap -> 3", text)
            self.assertIn("stderr: boom", text)
            # stdout can carry transcript text, which must stay out of logs.
            self.assertNotIn("TRANSCRIPT-TEXT", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class CliTest(unittest.TestCase):
    """The installer and rollback command lines reject bad usage."""

    def test_a_mode_is_required_and_a_foreign_home_is_dry_run_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--repo", tmp, "--sha", "0" * 40]
            for argv in (base, [*base, "--fresh", "--home", tmp]):
                with self.subTest(argv=argv):
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit) as cm,
                    ):
                        co.main(argv)
                    self.assertEqual(cm.exception.code, 2)
            record = str(Path(tmp) / "none.json")
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                rb.main(["--record", record, "--x"])


class CodexContractTest(unittest.TestCase):
    """The shipped Codex files match what the real Codex computes."""

    def test_hash_matches_real_codex_0_159_2(self) -> None:
        data = (ROOT / "integrations/codex/hooks/hooks.json").read_bytes()
        got = {h["suffix"]: h["hash"] for h in co.codex_hooks(data)}
        self.assertEqual(got, REAL_CODEX)

    def test_marketplace_catalog_names_plugin_0_2_0(self) -> None:
        root = ROOT / "integrations/codex"
        cat = json.loads(
            (root / ".agents/plugins/marketplace.json").read_text()
        )
        self.assertEqual(cat["name"], co.MKT_NAME)
        (entry,) = cat["plugins"]
        self.assertEqual(entry["name"], "provenance-context")
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        manifest = root / entry["source"]["path"] / ".codex-plugin/plugin.json"
        got = json.loads(manifest.read_text())
        self.assertEqual(
            (got["name"], got["version"]), ("provenance-context", "0.2.0")
        )


class GitEnvTest(unittest.TestCase):
    """The installer's git calls ignore a repository set by the caller."""

    def test_repository_variables_from_a_hook_are_not_inherited(self) -> None:
        env = dict(
            os.environ, GIT_INDEX_FILE="x", GIT_DIR="y", GIT_WORK_TREE="z"
        )
        code = "from install.constants import GIT_ENV; print(sorted(GIT_ENV))"
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env=env,
        ).stdout
        for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
            self.assertNotIn(name, out)
        self.assertIn("GIT_OPTIONAL_LOCKS", out)
