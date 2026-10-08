"""A real CI-only user manager whose home, unit paths and bus are isolated."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

from muninn import platform_io
from tests.native_diagnostics import write

_VENDOR_UNITS = (
    "basic.target",
    "sockets.target",
    "timers.target",
    "paths.target",
    "shutdown.target",
    "exit.target",
    "systemd-exit.service",
)


def copy_vendor_units(source: Path, destination: Path) -> None:
    """Copy only the required standard user-unit chain into private CI state.

    Args:
        source: Distribution systemd user-unit directory.
        destination: Private isolated unit directory without ambient wants.

    Raises:
        OSError: If a required ordinary read-only unit is unavailable.
    """
    for name in _VENDOR_UNITS:
        info = (source / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise PermissionError("ordinary read-only vendor unit required")
    platform_io.ensure_private_dir(destination)
    for name in _VENDOR_UNITS:
        fd = platform_io.open_private(
            destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
        with os.fdopen(fd, "wb") as output:
            output.write((source / name).read_bytes())


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
    updates = manager_environment(parent)
    runtime = Path(updates["XDG_RUNTIME_DIR"])
    original = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
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
        primary: Exception | None = None
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
        except Exception as exc:
            primary = exc
            write("lifecycle", None, error=exc)
            try:
                failure(parent, process.pid, process.poll(), started)
                print(json.dumps(service_codes()), flush=True)
            except Exception as diagnostic_error:
                print(
                    json.dumps(
                        {
                            "code": "diagnostic_failed",
                            "error_type": type(diagnostic_error).__name__,
                        }
                    ),
                    flush=True,
                )
            raise
        finally:
            try:
                finish_manager(process, primary)
            finally:
                log.close()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def manager_environment(parent: Path) -> dict[str, str]:
    """Prepare private paths and the finite distribution user-unit chain."""
    home, runtime = parent / "home", parent / "runtime"
    units = home / ".config/systemd/user"
    for path in (home, runtime, units):
        platform_io.ensure_private_dir(path)
    vendor = parent / "vendor-units"
    sources = (Path("/usr/lib/systemd/user"), Path("/lib/systemd/user"))
    source = next(
        (
            p
            for p in sources
            if all((p / name).is_file() for name in _VENDOR_UNITS)
        ),
        None,
    )
    if source is None:
        raise FileNotFoundError("required distribution user units unavailable")
    copy_vendor_units(source, vendor)
    (units / "default.target").write_text(
        "[Unit]\nDescription=Synthetic isolated test session\n"
    )
    return {
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "SYSTEMD_UNIT_PATH": str(units) + ":" + str(vendor),
        "SYSTEMD_LOG_TARGET": "journal",
        "SYSTEMD_LOG_LEVEL": "debug",
        "SYSTEMD_LOG_LOCATION": "1",
    }


def finish_manager(
    process: subprocess.Popen[bytes], primary: Exception | None
) -> None:
    """Retain an earlier lifecycle failure if safe manager exit also fails."""
    try:
        stop_manager(process)
    except Exception as exc:
        write("lifecycle", None, error=exc)
        print(
            json.dumps(
                {
                    "code": "manager_cleanup_failed",
                    "error_type": type(exc).__name__,
                    "pid": process.pid,
                    "alive": process.poll() is None,
                }
            ),
            flush=True,
        )
        if primary is None:
            raise


def stop_manager(process: subprocess.Popen[bytes]) -> None:
    """Request the owned user manager's standard exit and await real exit."""
    if process.poll() is not None:
        return
    result = subprocess.run(
        ["systemctl", "--user", "exit"], capture_output=True, timeout=30
    )
    print(
        json.dumps(
            {
                "code": "manager_exit",
                "returncode": result.returncode,
                "pid": process.pid,
                "alive": process.poll() is None,
            }
        ),
        flush=True,
    )
    result.check_returncode()
    process.wait(timeout=30)


def service_codes() -> dict[str, str | int]:
    """Read only fixed state codes for the synthetic Muninn unit."""
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            "muninn.service",
            "--property=LoadState,ActiveState,Result,MainPID",
        ],
        capture_output=True,
        timeout=5,
    )
    codes: dict[str, str | int] = {
        "code": "synthetic_service_state",
        "returncode": result.returncode,
    }
    allowed = {
        "loaded",
        "not-found",
        "error",
        "masked",
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "success",
        "exit-code",
        "signal",
        "timeout",
        "dependency",
        "resources",
        "start-limit-hit",
    }
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key == "MainPID" and value.isdigit() and len(value) < 11:
            codes[key] = int(value)
        elif key in {"LoadState", "ActiveState", "Result"}:
            codes[key] = value if value in allowed else "unknown"
    return codes


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
