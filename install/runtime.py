"""Provision and validate the pinned dependency closure before activation."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from install.context import Ctx, StepFailedError, dry

BOOTSTRAP = """
from importlib.metadata import version
from pydantic import BaseModel, ConfigDict, ValidationError
import sys

assert sys.version_info >= (3, 13)
for name, expected in (
    ("pydantic", "2.14.0"), ("pydantic-core", "2.50.0"),
    ("annotated-types", "0.8.0"), ("typing-extensions", "4.16.0"),
    ("typing-inspection", "0.4.4"),
):
    assert version(name) == expected

class Probe(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: int

schema = Probe.model_json_schema()
assert schema["additionalProperties"] is False
assert schema["properties"]["value"]["type"] == "integer"
assert Probe.model_validate_json('{"value":1}', strict=True).value == 1
for invalid in ('{"value":"1"}', '{"value":1,"extra":2}'):
    try:
        Probe.model_validate_json(invalid, strict=True)
    except ValidationError:
        pass
    else:
        raise RuntimeError("runtime_schema_not_strict")
"""


def verify_runtime(python: Path, release: Path) -> None:
    """Require exact installed pins, strict validation and schema generation.

    Args:
        python: Candidate release interpreter.
        release: Candidate source directory, used only as subprocess cwd.

    Raises:
        StepFailedError: If bootstrap execution or validation fails.
    """
    try:
        result = subprocess.run(
            [python, "-I", "-B", "-c", BOOTSTRAP],
            cwd=release,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailedError("runtime_bootstrap_unavailable") from exc
    if result.returncode:
        # Raw validation errors can contain input; only a fixed code escapes.
        raise StepFailedError("runtime_bootstrap_failed")


def runtime_python(release: Path, platform: str) -> Path:
    """Return the interpreter inside a final release's virtual environment."""
    relative = "Scripts/python.exe" if platform == "win32" else "bin/python3"
    return release / ".venv" / relative


def prepare_runtime(ctx: Ctx, release: Path) -> Path:
    """Install hashed wheels at the final path and verify before activation.

    Args:
        ctx: Installer context with isolated command and home boundaries.
        release: Immutable final release directory; never moved afterwards.

    Returns:
        The verified release interpreter.

    Raises:
        StepFailedError: If a lock is missing or provisioning/validation fails.
    """
    python = runtime_python(release, ctx.platform)
    if dry(ctx, f"would provision and verify the pinned runtime at {release}"):
        return python
    lock = release / "requirements-runtime.lock"
    if not lock.is_file():
        raise StepFailedError("runtime_lock_missing")
    if (release / ".venv").exists():
        # Cached releases are immutable, including a failed partial candidate.
        verify_runtime(python, release)
        return python
    env = dict(os.environ, HOME=str(ctx.home), USERPROFILE=str(ctx.home))
    commands = (
        [sys.executable, "-I", "-B", "-m", "venv", release / ".venv"],
        [
            python,
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--require-hashes",
            "--only-binary=:all:",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            lock,
        ],
    )
    try:
        for argv in commands:
            if ctx.run(argv, env=env).returncode:
                raise StepFailedError("runtime_provision_failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailedError("runtime_provision_unavailable") from exc
    verify_runtime(python, release)
    return python


def retired_runtimes(ctx: Ctx) -> list[Path]:
    """Find inactive provisioned releases for deletion after rollback ends."""
    active = ctx.release.resolve()
    return [
        path
        for path in ctx.lib.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and re.fullmatch(r"[0-9a-f]{40}", path.name)
        and path.resolve() != active
        and (path / ".venv").exists()
    ]
