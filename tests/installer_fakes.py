"""World-owned patch registration and fake runtime effects."""

from __future__ import annotations

import subprocess
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock


def register(tc: unittest.TestCase, home: Path) -> None:
    """Register fake installer boundaries for one owned World.

    Args:
        tc: Test case that owns patch cleanup.
        home: The World home, assigned before registration.
    """
    runtime = mock.patch("install.runtime.verify_runtime")
    runtime.start()
    tc.addCleanup(runtime.stop)
    identity = mock.patch(
        "install.steps_config.codex_identity",
        return_value=("synthetic-codex-candidate",),
    )
    identity.start()
    tc.addCleanup(identity.stop)


def provision_runtime(home: Path, argv: list[str]) -> bool:
    """Model venv/pip effects only inside the fixture's owned home."""
    if argv[1:5] == ["-I", "-B", "-m", "venv"]:
        venv = Path(argv[5])
        assert venv.is_relative_to(home), argv
        python = venv / "bin/python3"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fixture interpreter")
        python.chmod(0o700)
        return True
    if argv[1:5] == ["-I", "-B", "-m", "pip"]:
        assert Path(argv[0]).is_relative_to(home), argv
        assert "--require-hashes" in argv, argv
        return True
    return False


def no_command(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Refuse any unintended external effect from the fixture context."""
    raise AssertionError("no external command is expected")
