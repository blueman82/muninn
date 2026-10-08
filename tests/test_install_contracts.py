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
from unittest import mock

from install import installer as co
from install import rollback as rb
from tests.installer_support import REAL_CODEX, ROOT


class InstallLogTest(unittest.TestCase):
    """The private install log retains codes and numeric status."""

    def test_log_has_codes_and_status_but_no_external_text(self) -> None:
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
            self.assertIn("install_progress", text)
            self.assertIn('"exit": 3', text)
            for value in ("hello", "launchctl", "bootstrap", "boom"):
                self.assertNotIn(value, text)
            # stdout can carry transcript text, which must stay out of logs.
            self.assertNotIn("TRANSCRIPT-TEXT", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsafe_log_original_is_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "install.log"
            path.write_bytes(b"original")
            if sys.platform == "win32":
                subprocess.run(
                    ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
                    check=True,
                    capture_output=True,
                )
            else:
                path.chmod(0o666)
            with self.assertRaises(OSError):
                co.install_log(path)
            self.assertEqual(path.read_bytes(), b"original")
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o666)

    def test_symlink_log_parent_is_refused_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            link = root / "link"
            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                    check=True,
                    capture_output=True,
                )
            else:
                link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                co.install_log(link / "install.log")
            self.assertEqual(list(outside.iterdir()), [])

    def test_failed_record_does_not_persist_external_error_text(self) -> None:
        rec: co.Record = {"steps": []}
        ctx = co.Ctx(
            Path("/synthetic/home"), co.run_real, "test", say=lambda text: None
        )
        with (
            mock.patch.object(co, "save"),
            mock.patch.object(co, "rollback", return_value=[]),
            mock.patch.object(co, "install_record"),
        ):
            co._roll_back(
                ctx,
                rec,
                co.pin,
                RuntimeError("external transcript and credential"),
            )
        self.assertEqual(
            rec["failed"], {"step": "pin", "error": "RuntimeError"}
        )


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
                        mock.patch.object(co.sys, "platform", "darwin"),
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

    def test_hash_matches_real_codex_0_160_0(self) -> None:
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
        self.assertEqual(entry["name"], "muninn")
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        manifest = root / entry["source"]["path"] / ".codex-plugin/plugin.json"
        got = json.loads(manifest.read_text())
        self.assertEqual((got["name"], got["version"]), ("muninn", "0.2.0"))


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
