"""A real CI-only user manager whose home, unit paths and bus are isolated."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
    if sys.platform != "linux":
        raise RuntimeError("isolated manager requires native Linux")
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
        "SYSTEMD_LOG_TARGET": "journal",
        "SYSTEMD_LOG_LEVEL": "debug",
        "SYSTEMD_LOG_LOCATION": "1",
    }
    original = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    executable = shutil.which("systemd")
    if executable is None:
        raise RuntimeError("systemd executable is unavailable")
    startup(runtime, executable)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    log = (parent / "manager.log").open("wb")
    process = subprocess.Popen(
        [
            executable,
            "--user",
            "--unit=default.target",
            "--log-target=journal",
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
                journal_status = failure(
                    parent, process.pid, process.poll(), started
                )
                raise RuntimeError(
                    f"isolated user manager exit={process.poll()}; "
                    f"journal_status={journal_status}"
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


def journal_codes(data: bytes) -> list[dict[str, str | int]]:
    """Extract bounded source and errno IDs without exporting journal text."""
    result: list[dict[str, str | int]] = []
    for line in data.splitlines()[-32:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        fields: dict[str, str | int] = {}
        for key in ("ERRNO", "CODE_LINE"):
            value = entry.get(key)
            if isinstance(value, str) and value.isdigit() and len(value) < 8:
                fields[key] = int(value)
        identifier = entry.get("MESSAGE_ID")
        if isinstance(identifier, str) and re.fullmatch(
            r"[0-9a-f]{32}", identifier
        ):
            fields["MESSAGE_ID"] = identifier
        source = entry.get("CODE_FILE")
        if isinstance(source, str):
            name = Path(source).name
            if name in {"main.c", "manager.c", "cgroup.c", "dbus.c", "log.c"}:
                fields["source_id"] = name
        if fields:
            result.append(fields)
    return result


def cgroup_owner(cgroup: bytes, root: Path, uid: int) -> int:
    """Require an ordinary user's delegated unified cgroup before startup.

    Args:
        cgroup: Kernel membership record for this process.
        root: Native cgroup filesystem root.
        uid: Expected ordinary process owner.

    Returns:
        Verified owner id.

    Raises:
        RuntimeError: If membership is not a confined unified cgroup.
        PermissionError: If its directory is not owned by this user.
    """
    records = cgroup.decode().splitlines()
    if len(records) != 1 or not records[0].startswith("0::/"):
        raise RuntimeError("unified cgroup membership is required")
    relative = Path(records[0][4:])
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("confined cgroup membership is required")
    directory = root / relative
    if not directory.is_dir() or directory.stat().st_uid != uid:
        raise PermissionError("ordinary user does not own delegated cgroup")
    return uid


def startup(runtime: Path, executable: str) -> None:
    """Emit native startup identity and counts without arbitrary output."""
    if sys.platform != "linux":
        raise RuntimeError("startup identity requires native Linux")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, timeout=5
    )
    cgroup = Path("/proc/self/cgroup").read_bytes()
    owner = cgroup_owner(cgroup, Path("/sys/fs/cgroup"), os.getuid())
    print(
        json.dumps(
            {
                "code": "manager_startup",
                "executable": str(Path(executable).resolve()),
                "version_status": version.returncode,
                "version_id": hashlib.sha256(version.stdout).hexdigest(),
                "uid": os.getuid(),
                "euid": os.geteuid(),
                "runtime_owner": runtime.stat().st_uid,
                "systemd_booted": Path("/run/systemd/system").is_dir(),
                "cgroup_id": hashlib.sha256(cgroup).hexdigest(),
                "cgroup_count": len(cgroup.splitlines()),
                "cgroup_owner": owner,
            }
        ),
        flush=True,
    )


def failure(
    parent: Path, pid: int, exit_status: int | None, started: str
) -> int:
    """Emit only source and errno IDs from this boot and synthetic process."""
    journal = subprocess.run(
        [
            "journalctl",
            "--boot=0",
            "--since",
            started,
            "--no-pager",
            "--output=json",
            f"_PID={pid}",
        ],
        capture_output=True,
        timeout=5,
    )
    details = journal_codes(journal.stdout)
    print(
        json.dumps(
            {
                "code": "manager_startup_failed",
                "pid": pid,
                "exit_status": exit_status,
                "journal_status": journal.returncode,
                "journal_bytes": len(journal.stdout),
                "journal_id": hashlib.sha256(journal.stdout).hexdigest(),
                "source_ids": details,
                "stderr_bytes": (parent / "manager.log").stat().st_size,
            }
        ),
        flush=True,
    )
    return journal.returncode
