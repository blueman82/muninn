"""Pre-rename names: old env vars are called out."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import cli, legacy
from muninn.erase_residue import residue_scan


class EnvWarningTests(unittest.TestCase):
    """Old PCTX_* variables are ignored, with one line saying so."""

    def test_names_each_old_variable_and_its_replacement(self) -> None:
        said = legacy.env_warning({"PCTX_HOME": "/x", "PCTX_ROOTS": "{}"})
        assert said is not None
        self.assertIn("PCTX_HOME (use MUNINN_HOME)", said)
        self.assertIn("PCTX_ROOTS (use MUNINN_ROOTS)", said)
        self.assertNotIn("\n", said)

    def test_silent_without_old_variables(self) -> None:
        self.assertIsNone(legacy.env_warning({"MUNINN_HOME": "/x"}))

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Run the CLI with PCTX_HOME set, capturing both streams."""
        out, err = io.StringIO(), io.StringIO()
        env = {"PCTX_HOME": "/nowhere", "MUNINN_HOOK_DISABLE": "1"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("sys.stdin", io.StringIO("{}")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_warns_on_stderr(self) -> None:
        code, _, err = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn("PCTX_HOME (use MUNINN_HOME)", err)

    def test_hooks_never_warn_and_print_compact_json(self) -> None:
        code, out, err = self.run_cli("hook", "prompt", "--provider", "claude")
        self.assertEqual((code, out.strip(), err), (0, "{}", ""))


class LegacyResidueTests(unittest.TestCase):
    """An un-migrated old data home beside the new one is residue."""

    def test_old_data_dir_with_erased_bytes_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "muninn"
            old = legacy.old_data_dir(home)
            home.mkdir()
            old.mkdir()
            (old / "pctx.sqlite").write_bytes(b"xx secret-canary-text xx")
            hits = residue_scan(home, [b"secret-canary-text"])
        self.assertEqual(hits, ["provenance-context/pctx.sqlite"])


if __name__ == "__main__":
    unittest.main()
