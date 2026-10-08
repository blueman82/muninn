"""Actual native installer and service smoke in synthetic isolated homes."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from install import installer, lifecycle
from install.context import Ctx, run_real
from muninn import obs_status, platform_io, tombstone_key
from muninn.obs_service import parse_process, process_command
from tests import lifecycle_native_linux, lifecycle_native_windows

ROOT = Path(__file__).resolve().parent.parent


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
    key = tombstone_key.load_key(ctx.data)
    lifecycle.stop(ctx)
    stopped = lifecycle.job(ctx)
    assert stopped is None or not stopped["pid"]
    upgraded = dataclasses.replace(
        ctx, ts="native-upgrade", fresh=False, upgrade=True
    )
    installer.install(upgraded, ROOT, sha)
    after_job = lifecycle.job(upgraded)
    assert after_job is not None
    assert isinstance(after_job["pid"], int) and after_job["pid"] > 0
    after = obs_status.read_status(ctx.data)
    assert after["pid"] == after_job["pid"]
    assert after["pid"] != first["pid"], (first["pid"], after["pid"])
    if sys.platform == "win32":
        assert after["stop_generation"] != first["stop_generation"]
    assert tombstone_key.load_key(ctx.data, create=False) == key
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


def main() -> int:
    """Run under a normal token and a private manager, never uninstall CLI."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    args = parser.parse_args()
    if args.child is not None:
        result: dict[str, object]
        try:
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
            result = {
                "ok": False,
                "error": type(exc).__name__,
                "detail": str(exc),
            }
        else:
            result["ok"] = True
        (args.child / "result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return 0 if result["ok"] else 1
    parent = Path(tempfile.mkdtemp(prefix="muninn-native-life-"))
    try:
        if sys.platform == "win32":
            result = lifecycle_native_windows.run_child(parent, ROOT)
        elif sys.platform == "linux":
            with lifecycle_native_linux.manager(parent):
                result = exercise(parent)
        else:
            raise RuntimeError("native lifecycle requires Windows or Linux")
    except Exception:
        print(json.dumps({"retained_state": str(parent), "pid": os.getpid()}))
        raise
    print(json.dumps(result))
    shutil.rmtree(parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
