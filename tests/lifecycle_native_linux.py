"""A real CI-only user manager whose home, unit paths and bus are isolated."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

from muninn import platform_io


@contextlib.contextmanager
def manager(parent: Path) -> Generator[None]:
    """Run systemd --user with an empty synthetic default target.

    Args:
        parent: Synthetic root owned by the current ordinary CI user.

    Yields:
        Nothing while the private manager is available.

    Raises:
        RuntimeError: If the actual user manager cannot run or exit safely.
    """
    home, runtime = parent / "home", parent / "runtime"
    units = home / ".config/systemd/user"
    for path in (home, runtime, units):
        platform_io.ensure_private_dir(path)
    (units / "default.target").write_text(
        "[Unit]\nDescription=Synthetic isolated test session\n"
    )
    updates = {
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "SYSTEMD_UNIT_PATH": str(units),
    }
    original = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    log = (parent / "manager.log").open("wb")
    process = subprocess.Popen(
        [
            "systemd",
            "--user",
            "--unit=default.target",
            "--log-target=console",
            "--log-level=debug",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        while subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            timeout=5,
        ).returncode:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"isolated user manager exit={process.poll()}: "
                    + (parent / "manager.log").read_text()
                )
            time.sleep(0.1)
        yield
    finally:
        if process.poll() is None:
            subprocess.run(
                ["systemctl", "--user", "exit"],
                capture_output=True,
                timeout=30,
                check=True,
            )
            process.wait(timeout=30)
        log.close()
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
