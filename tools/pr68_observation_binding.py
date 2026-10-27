"""Bind carrier inputs outside the original supervised timing regions."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE = "71e832c2f81cf16e537f3b653b6d1c9984ee55cf"
ROOT = Path(__file__).resolve().parent.parent


def digest(path: Path) -> str:
    """Hash one explicitly identified source or interpreter file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*arguments: str) -> bytes:
    """Run an explicit Git command in the selected preparation checkout."""
    return subprocess.check_output(
        ["git", "--no-optional-locks", *arguments], cwd=ROOT
    )


def loaded_paths() -> dict[str, str]:
    """Copy current module filenames without IO or another import."""
    return {
        name: str(filename)
        for name, module in tuple(sys.modules.items())
        if isinstance(filename := getattr(module, "__file__", None), str)
    }


def loaded_hashes(paths: dict[str, str]) -> dict[str, dict[str, str]]:
    """Bind recorded loaded paths after timed supervision has unwound."""
    return {
        name: {"path": filename, "sha256": digest(Path(filename))}
        for name, filename in paths.items()
        if Path(filename).is_file()
    }


def write(name: str, value: object) -> None:
    """Create an independent artifact without replacing earlier evidence."""
    destination = Path(os.environ["MUNINN_CLEANUP_OBSERVATION_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / name).open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")


def prepare() -> None:
    """Bind source, interpreter and startup inputs before the gate."""
    destination = Path(os.environ["MUNINN_CLEANUP_OBSERVATION_DIR"])
    overlay = json.loads((destination / "overlay.json").read_text())
    if (
        git("rev-parse", "HEAD").decode().strip() != BASE
        or str(ROOT) != overlay["diagnostic_worktree"]
        or hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest()
        != overlay["diagnostic_index_digest"]
    ):
        raise ValueError("diagnostic_identity_drift")
    git("diff", "--exit-code")
    for name, expected in overlay["copied"].items():
        if digest(ROOT / name) != expected:
            raise ValueError("diagnostic_source_drift")
    names = git("ls-files", "-z").decode().split("\0")
    source = {name: digest(ROOT / name) for name in names if name}
    startup = {
        key: {
            "present": key in os.environ,
            "sha256": hashlib.sha256(
                os.environ.get(key, "").encode()
            ).hexdigest(),
        }
        for key in (
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONUTF8",
            "PYTHONUNBUFFERED",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
        )
    }
    write(
        "before.json",
        {
            "base": BASE,
            "carrier_head": overlay["carrier_head"],
            "carrier_tree": overlay["carrier_tree"],
            "diagnostic_head": git("rev-parse", "HEAD").decode().strip(),
            "diagnostic_head_tree": git("rev-parse", "HEAD^{tree}")
            .decode()
            .strip(),
            "diagnostic_index_tree": overlay["diagnostic_index_tree"],
            "diagnostic_index_digest": overlay["diagnostic_index_digest"],
            "source": source,
            "loaded": loaded_hashes(loaded_paths()),
            "executable": str(Path(sys.executable).resolve()),
            "executable_sha256": digest(Path(sys.executable).resolve()),
            "prefix": sys.prefix,
            "venv_link_target": overlay["venv_link_target"],
            "cache_effect": overlay["cache_effect"],
            "python": sys.version,
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
            "packages": {
                item.metadata["Name"]: item.version
                for item in importlib.metadata.distributions()
            },
            "startup": startup,
            "default_workers": min(4, os.cpu_count() or 1),
        },
    )


def finish() -> None:
    """Retain actual post-suite source equality and missing-trace evidence."""
    destination = Path(os.environ["MUNINN_CLEANUP_OBSERVATION_DIR"])
    before = json.loads((destination / "before.json").read_text())
    names = git("ls-files", "-z").decode().split("\0")
    source = {name: digest(ROOT / name) for name in names if name}
    write(
        "after.json",
        {
            "source": source,
            "source_unchanged": source == before["source"],
            "diagnostic_head": git("rev-parse", "HEAD").decode().strip(),
            "diagnostic_index_digest": hashlib.sha256(
                git("ls-files", "--stage", "-z")
            ).hexdigest(),
            "trace_present": (destination / "trace.json").is_file(),
            "parent_loaded_present": (
                destination / "parent-loaded.json"
            ).is_file(),
            "missing_trace_means": "incomplete_diagnostics",
            "native_acceptance_claim": False,
        },
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["after"]:
        finish()
    elif not sys.argv[1:]:
        prepare()
    else:
        raise ValueError("unsupported_binding_operation")
