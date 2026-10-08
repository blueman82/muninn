"""Actual native installer and service smoke in synthetic isolated homes."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from install import installer, lifecycle
from install.context import Ctx, run_real
from muninn import obs_status, platform_io, tombstone_key
from muninn.obs_service import parse_process, process_command, service_status
from tests import lifecycle_native_linux, lifecycle_native_windows
from tests.native_diagnostics import write

ROOT = Path(__file__).resolve().parent.parent


def assert_service(ctx: Ctx, sha: str) -> None:
    """Require a real Linux service identity and its immutable loaded SHA."""
    if sys.platform == "linux":
        status = obs_status.read_status(ctx.data)
        assert status.get("writer_install_sha") == sha
        assert service_status(ctx.data, {"HOME": str(ctx.home)}, run_real) == (
            True,
            status["pid"],
        )


def exercise(parent: Path) -> dict[str, object]:
    """Run real fresh, graceful stop and upgrade with no provider configs."""
    home = parent / "home"
    platform_io.ensure_private_dir(home)
    sha = (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    messages: list[str] = []
    ctx = Ctx(home, run_real, "native-fresh", fresh=True, say=messages.append)
    started = time.monotonic()
    write("lifecycle", "fresh_install")
    installer.install(ctx, ROOT, sha)
    first_job = lifecycle.job(ctx)
    assert first_job is not None
    assert isinstance(first_job["pid"], int) and first_job["pid"] > 0
    first = obs_status.read_status(ctx.data)
    assert first["pid"] == first_job["pid"]
    assert isinstance(first.get("pid"), int), first
    if sys.platform == "linux":
        library = Path(os.environ["LD_LIBRARY_PATH"]) / "libsqlite3.so.0"
        maps = Path(f"/proc/{first['pid']}/maps").read_text()
        assert str(library.resolve()) in maps, "writer SQLite environment"
    assert_service(ctx, sha)
    key = tombstone_key.load_key(ctx.data)
    write("lifecycle", "fresh_stop")
    lifecycle.stop(ctx)
    stopped = lifecycle.job(ctx)
    assert stopped is None or not stopped["pid"]
    upgraded = dataclasses.replace(
        ctx, ts="native-upgrade", fresh=False, upgrade=True
    )
    write("lifecycle", "upgrade_install")
    installer.install(upgraded, ROOT, sha)
    after_job = lifecycle.job(upgraded)
    assert after_job is not None
    assert isinstance(after_job["pid"], int) and after_job["pid"] > 0
    after = obs_status.read_status(ctx.data)
    assert after["pid"] == after_job["pid"]
    assert after["pid"] != first["pid"], (first["pid"], after["pid"])
    assert_service(upgraded, sha)
    if sys.platform == "win32":
        assert after["stop_generation"] != first["stop_generation"]
    assert tombstone_key.load_key(ctx.data, create=False) == key
    write("lifecycle", "upgrade_stop")
    lifecycle.stop(upgraded)
    result = {
        "sha": sha,
        "platform": sys.platform,
        "fresh": True,
        "upgrade": True,
        "graceful_stop": True,
        "key_retained": True,
        "native_seconds": round(time.monotonic() - started, 3),
    }
    if sys.platform == "win32":
        deleted = run_real(
            ["schtasks.exe", "/Delete", "/TN", upgraded.target, "/F"]
        )
        assert deleted.returncode == 0
    else:
        ctx.plist.unlink()
        run_real(["systemctl", "--user", "daemon-reload"])
    return result


def refused_main(parent: Path) -> None:
    """Prove the real CLI refuses unsafe preflight without state writes."""
    write("lifecycle", "refused_cli")
    sha = (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    home = parent / "refused-home"
    env = dict(os.environ)
    if sys.platform == "linux":
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(
            parent / "missing-bus"
        )
        env["XDG_RUNTIME_DIR"] = str(parent / "missing-runtime")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "install.installer",
            "--repo",
            str(ROOT),
            "--sha",
            sha,
            "--fresh",
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 1, result.returncode
    expected = (
        b"systemctl --user exited"
        if sys.platform == "linux"
        else b"run the installer as an ordinary Windows user"
    )
    assert expected in result.stdout, "CLI refused at an unexpected preflight"
    assert not home.exists(), "refused CLI created state"


def main() -> int:
    """Run under a normal token and a private manager, never uninstall CLI."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    args = parser.parse_args()
    if args.child is not None:
        result: dict[str, object]
        try:
            write(
                "lifecycle",
                "ordinary_child_identity",
                metrics=lifecycle_native_windows.token_codes(),
            )
            ordinary = lifecycle._identity(
                Ctx(args.child, run_real, "identity")
            )
            identity = parse_process(
                run_real(process_command(os.getpid())).stdout
            )
            result = exercise(args.child)
            result["child_pid"] = identity["pid"]
            result["child_created"] = identity["created"]
            result["ordinary_user_sid"] = ordinary
            result["elevated"] = False
        except Exception as exc:
            write("lifecycle", None, error=exc)
            result = {
                "ok": False,
                "error": type(exc).__name__,
                "winerror": getattr(exc, "winerror", None),
            }
        else:
            result["ok"] = True
        (args.child / "result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return 0 if result["ok"] else 1
    temporary = tempfile.TemporaryDirectory(
        prefix="muninn-native-life-", delete=False
    )
    parent = Path(temporary.name)
    try:
        refused_main(parent)
        if sys.platform == "win32":
            write("lifecycle", "ordinary_child_start")
            result = lifecycle_native_windows.run_child(parent, ROOT)
        elif sys.platform == "linux":
            write("lifecycle", "manager_start")
            with lifecycle_native_linux.manager(parent):
                result = exercise(parent)
        else:
            raise RuntimeError("native lifecycle requires Windows or Linux")
        temporary.cleanup()
    except Exception as exc:
        write("lifecycle", None, error=exc)
        print(json.dumps({"retained_state": str(parent), "pid": os.getpid()}))
        raise
    write("lifecycle", "complete", completed=True)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
