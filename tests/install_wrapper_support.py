"""Temporary checkout and interpreter fixtures for installer wrapper tests."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.installer_support import ROOT, git

WRAPPER = ROOT / "bin/muninn-install"


class WrapperFixture(unittest.TestCase):
    """Run the wrapper in a temp HOME with a stub in place of Python."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.log = Path(tmp.name) / "argv"
        self.stub = Path(tmp.name) / "python-stub"
        self.stub.write_text(
            '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then '
            'if [ "${VENV_DIR:-bin}" = "Scripts" ]; then '
            'mkdir -p "$3/Scripts"; cp "$0" "$3/Scripts/python.exe"; '
            'else mkdir -p "$3/bin"; cp "$0" "$3/bin/python3"; '
            'cp "$0" "$3/bin/python3.13"; fi; exit 0; fi\n'
            'while [ "${1:-}" = -E ] || [ "${1:-}" = -s ] || '
            '[ "${1:-}" = -B ]; do shift; done\n'
            'if [ "$1" = "-c" ] && '
            '[ "$2" = "import sys; print(sys.platform)" ]; then '
            'echo "${PY_PLATFORM:-linux}"; exit 0; fi\n'
            'if [ "$1" = "-c" ]; then '
            'if [ "$0" = "$MUNINN_PYTHON" ] && '
            '[ "${IMPORT_RC:-0}" -ne 0 ]; then exit "$IMPORT_RC"; fi\n'
            'exit "${PINNED_VERSION_RC:-0}"; fi\n'
            f'echo "$@" >> {shlex.quote(self.log.as_posix())}\n'
            'exit "${STUB_RC:-0}"\n'
        )
        self.stub.chmod(0o755)
        self.sha = git(ROOT, "rev-parse", "HEAD").decode().strip()
        self.lib = self.home / ".local/lib/muninn"
        self.data = self.home / ".local/share/muninn"

    def run_wrapper(
        self,
        *args: str,
        rc: int = 0,
        script: Path = WRAPPER,
        input: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``bin`` script against the temp HOME.

        Args:
            *args: Command-line arguments.
            rc: Exit status the stub Python should return.
            script: The script to run, ``bin/muninn-install`` by default.
            input: Text sent to the wrapper's stdin.
            extra_env: Additional variables for the wrapper process.

        Returns:
            The finished process, with text output.
        """
        env = dict(
            os.environ,
            HOME=str(self.home),
            MUNINN_PYTHON=str(self.stub),
            STUB_RC=str(rc),
        )
        env.update(extra_env or {})
        command: list[str | Path] = [script, *args]
        if sys.platform == "win32":
            shell = shutil.which("sh")
            if shell is None:
                raise RuntimeError("Windows wrapper fixture needs required sh")
            command = [shell, script.as_posix(), *args]
            env.update(
                HOME=self.home.as_posix(),
                MUNINN_PYTHON=(
                    Path(env["MUNINN_PYTHON"]).as_posix()
                    if env["MUNINN_PYTHON"]
                    else ""
                ),
            )
        return subprocess.run(
            command,
            env=env,
            input=input,
            capture_output=True,
            text=True,
        )

    def installed(self, sha: str) -> None:
        """Make the temp HOME look like a machine running release ``sha``."""
        (self.lib / sha).mkdir(parents=True)
        (self.lib / "current").symlink_to(sha)
        self.data.mkdir(parents=True)

    def argv(self) -> str:
        """Return what the stub Python was run with, or empty if never."""
        return self.log.read_text() if self.log.exists() else ""

    def hook_repo(self) -> Path:
        """Create a temporary checkout for Git-hook setup tests."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "bin").mkdir()
        script = root / "bin" / "muninn-install"
        shutil.copyfile(WRAPPER, script)
        script.chmod(0o755)
        shutil.copyfile(
            ROOT / "requirements-runtime.lock",
            root / "requirements-runtime.lock",
        )
        shutil.copyfile(
            ROOT / "requirements-dev.txt", root / "requirements-dev.txt"
        )
        git(root, "init", "-q")
        git(root, "commit", "--allow-empty", "-qm", "initial")
        return root
