"""Codex native executable and npm dispatch retain structured arguments."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from install.context import Ctx, run_real
from install.provider_paths import codex_argv
from muninn import platform_io


class CodexCommandTest(unittest.TestCase):
    """Unsafe candidates refuse and npm dispatch avoids cmd execution."""

    def test_direct_native_codex_is_selected(self) -> None:
        ctx = Ctx(Path("/temporary"), run_real, "test", platform="win32")
        with (
            mock.patch(
                "install.provider_paths.shutil.which",
                return_value="C:/safe/codex.exe",
            ),
            mock.patch(
                "install.provider_paths._windows_executable",
            ) as check,
        ):
            self.assertEqual(
                codex_argv(ctx, "--version"),
                ["C:/safe/codex.exe", "--version"],
            )
        check.assert_called_once()

    def test_unsafe_direct_codex_does_not_fallback(self) -> None:
        ctx = Ctx(Path("/temporary"), run_real, "test", platform="win32")
        with (
            mock.patch(
                "install.provider_paths.shutil.which",
                return_value="C:/unsafe/codex.exe",
            ),
            mock.patch(
                "install.provider_paths._windows_executable",
                side_effect=OSError("unsafe"),
            ),
            self.assertRaises(OSError),
        ):
            codex_argv(ctx, "--version")

    def test_official_npm_script_roundtrip_uses_real_node(self) -> None:
        node = shutil.which("node.exe") or shutil.which("node")
        self.assertIsNotNone(
            node, "installed Node required for native npm dispatch proof"
        )
        assert node is not None
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp).resolve() / "space café owner's % $ prefix"
            platform_io.ensure_private_dir(prefix)
            if sys.platform == "win32":
                private_node = prefix / "node.exe"
                shutil.copyfile(node, private_node)
                node = str(private_node)
            script = prefix / "node_modules/@openai/codex/bin/codex.js"
            script.parent.mkdir(parents=True)
            script.write_text(
                "console.log(JSON.stringify(process.argv.slice(2))); "
                "process.exit(23);",
                encoding="utf-8",
            )
            shim = prefix / "codex.cmd"
            shim.write_text("never execute this cmd shim")
            ctx = Ctx(prefix, run_real, "test", platform="win32")
            candidates = {
                "codex.exe": None,
                "codex": str(shim),
                "node.exe": node,
            }
            validation = (
                nullcontext()
                if sys.platform == "win32"
                else mock.patch(
                    "install.provider_paths._windows_executable",
                )
            )
            with (
                mock.patch(
                    "install.provider_paths.shutil.which",
                    side_effect=lambda name: candidates[name],
                ),
                validation,
            ):
                arguments = [
                    'quote"text',
                    "",
                    "café 雪",
                    "% & | $ `",
                    "trailing\\",
                ]
                argv = codex_argv(ctx, *arguments)
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(done.returncode, 23, done.stderr)
            self.assertEqual(json.loads(done.stdout), arguments)
